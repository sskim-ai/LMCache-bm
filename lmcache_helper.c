#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/statvfs.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static double now_sec(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static long rand_range(long min_ms, long max_ms) {
    if (max_ms <= min_ms) return min_ms;
    return min_ms + (rand() % (max_ms - min_ms + 1));
}

static void ms_sleep(long ms) {
    if (ms <= 0) return;
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

static size_t get_alignment(const char* root) {
    struct statvfs st;
    if (statvfs(root, &st) == 0 && st.f_bsize > 0) {
        return (size_t)st.f_bsize;
    }
    return 4096;
}

/* Read path: caller allocates aligned buffer once and reuses it across files */
static int read_full_file_direct_reuse_buf(
    const char* path,
    void* buf,
    size_t bs,
    uint64_t* total_bytes,
    uint64_t* total_ops
) {
    int fd = open(path, O_RDONLY | O_DIRECT);
    if (fd < 0) return -1;

    while (1) {
        ssize_t n = read(fd, buf, bs);
        if (n < 0) {
            close(fd);
            return -1;
        }
        if (n == 0) break;
        *total_bytes += (uint64_t)n;
        *total_ops += 1;
    }

    close(fd);
    return 0;
}

/* Write path: caller allocates aligned zero buffer once and reuses it across files */
static int write_full_file_direct_reuse_buf(
    const char* path,
    void* buf,
    size_t file_size,
    uint64_t* total_bytes,
    uint64_t* total_ops
) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_DIRECT, 0644);
    if (fd < 0) return -1;

    size_t written = 0;
    while (written < file_size) {
        ssize_t n = write(fd, (char*)buf + written, file_size - written);
        if (n < 0) {
            close(fd);
            return -1;
        }
        written += (size_t)n;
    }

    /* Match LMCache LocalDiskBackend more closely: no fsync here */
    *total_bytes += (uint64_t)written;
    *total_ops += 1;

    close(fd);
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 11) {
        fprintf(stderr, "usage: %s <mode> <root_dir> <tp> <job_or_-1> <num_files> <file_size_bytes> <bs_bytes_or_-1> <runtime_sec> <delay_min_ms> <delay_max_ms>\n", argv[0]);
        return 2;
    }

    const char* mode = argv[1];
    const char* root_dir = argv[2];
    int tp = atoi(argv[3]);
    int job = atoi(argv[4]);
    int num_files = atoi(argv[5]);
    size_t file_size = (size_t)strtoull(argv[6], NULL, 10);
    size_t bs = (size_t)strtoull(argv[7], NULL, 10);
    double runtime = atof(argv[8]);
    long delay_min_ms = atol(argv[9]);
    long delay_max_ms = atol(argv[10]);

    srand((unsigned int)(time(NULL) ^ getpid() ^ tp ^ (job << 8)));
    size_t alignment = get_alignment(root_dir);

    double start = now_sec();
    uint64_t total_bytes = 0;
    uint64_t total_ops = 0;
    uint64_t passes = 0;
    uint64_t delays_count = 0;
    uint64_t delays_sum_ms = 0;

    char path[4096];

    if (strcmp(mode, "read") == 0) {
        if (bs == 0 || bs == (size_t)-1) bs = file_size;

        void* read_buf = NULL;
        int rc = posix_memalign(&read_buf, alignment, bs);
        if (rc != 0) {
            errno = rc;
            fprintf(stderr, "posix_memalign(read) failed errno=%d %s\n", errno, strerror(errno));
            return 1;
        }

        while (now_sec() - start < runtime) {
            for (int i = 0; i < num_files; i++) {
                if (now_sec() - start >= runtime) break;
                snprintf(path, sizeof(path), "%s/tp%d/file_%d.bin", root_dir, tp, i);
                if (read_full_file_direct_reuse_buf(path, read_buf, bs, &total_bytes, &total_ops) != 0) {
                    fprintf(stderr, "read_full_file_direct_reuse_buf failed path=%s errno=%d %s\n", path, errno, strerror(errno));
                    free(read_buf);
                    return 1;
                }
            }
            passes++;
            if (now_sec() - start >= runtime) break;
            long d = rand_range(delay_min_ms, delay_max_ms);
            delays_count++;
            delays_sum_ms += (uint64_t)d;
            ms_sleep(d);
        }

        free(read_buf);

    } else if (strcmp(mode, "write") == 0) {
        if (job < 0) {
            fprintf(stderr, "write mode requires job >= 0\n");
            return 2;
        }

        void* write_buf = NULL;
        int rc = posix_memalign(&write_buf, alignment, file_size);
        if (rc != 0) {
            errno = rc;
            fprintf(stderr, "posix_memalign(write) failed errno=%d %s\n", errno, strerror(errno));
            return 1;
        }
        memset(write_buf, 0, file_size);

        while (now_sec() - start < runtime) {
            for (int i = 0; i < num_files; i++) {
                if (now_sec() - start >= runtime) break;
                snprintf(path, sizeof(path), "%s/tp%d/job%d/file_%d.bin", root_dir, tp, job, i);
                if (write_full_file_direct_reuse_buf(path, write_buf, file_size, &total_bytes, &total_ops) != 0) {
                    fprintf(stderr, "write_full_file_direct_reuse_buf failed path=%s errno=%d %s\n", path, errno, strerror(errno));
                    free(write_buf);
                    return 1;
                }
                if (now_sec() - start >= runtime) break;
                long d = rand_range(delay_min_ms, delay_max_ms);
                delays_count++;
                delays_sum_ms += (uint64_t)d;
                ms_sleep(d);
            }
            passes++;
        }

        free(write_buf);

    } else {
        fprintf(stderr, "unknown mode: %s\n", mode);
        return 2;
    }

    double wall = now_sec() - start;
    double gbps = wall > 0 ? ((double)total_bytes / 1e9) / wall : 0.0;
    double iops = wall > 0 ? ((double)total_ops) / wall : 0.0;
    double avg_delay_ms = delays_count ? ((double)delays_sum_ms / (double)delays_count) : 0.0;

    printf("{\"mode\":\"%s\",\"tp\":%d,\"job\":%d,\"num_files\":%d,"
           "\"file_size_bytes\":%" PRIu64 ",\"bs_bytes\":%" PRIu64 ","
           "\"passes\":%" PRIu64 ",\"total_bytes\":%" PRIu64 ",\"total_ops\":%" PRIu64 ","
           "\"wall_sec\":%.6f,\"throughput_GBps\":%.6f,\"IOPS\":%.6f,"
           "\"delays_count\":%" PRIu64 ",\"avg_delay_ms\":%.3f}\n",
           mode, tp, job, num_files, (uint64_t)file_size, (uint64_t)bs,
           passes, total_bytes, total_ops, wall, gbps, iops, delays_count, avg_delay_ms);
    return 0;
}
