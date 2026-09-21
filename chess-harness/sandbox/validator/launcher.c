#define _GNU_SOURCE

/* Trusted container supervisor. Generated code runs only in the child. */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define STDOUT_LIMIT (64U * 1024U)
#define STDERR_LIMIT (16U * 1024U)
#define WALL_SECONDS 2.0
#define REAP_GRACE_SECONDS 0.5
#define BOOTSTRAP_FAILURE 24

struct stream {
    int fd;
    unsigned char *data;
    size_t capacity;
    size_t retained;
    uint64_t observed;
};

static double monotonic_seconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        return -1.0;
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
}

static void terminate_worker(pid_t child)
{
    /* Both sides of fork establish this process group before exec. */
    (void)kill(-child, SIGKILL);
    (void)kill(child, SIGKILL);
}

static int close_child_descriptors(void)
{
#ifdef SYS_close_range
    if (syscall(SYS_close_range, 4U, UINT_MAX, 0U) == 0)
        return 0;
#endif
    /* This runs before any generated code, on older Linux kernels only. */
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit) != 0 || limit.rlim_cur == RLIM_INFINITY)
        return -1;
    for (rlim_t fd = 4; fd < limit.rlim_cur && fd <= INT_MAX; ++fd)
        (void)close((int)fd);
    return 0;
}

static void child_bootstrap_failed(int fd)
{
    int failure = errno == 0 ? EIO : errno;
    ssize_t written;
    do {
        written = write(fd, &failure, sizeof(failure));
    } while (written < 0 && errno == EINTR);
    _exit(BOOTSTRAP_FAILURE);
}

static int nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL);
    return flags == -1 ? -1 : fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

/* A single bounded read prevents a continuous writer starving the deadline. */
static int drain_once(struct stream *stream)
{
    unsigned char chunk[8192];
    ssize_t count = read(stream->fd, chunk, sizeof(chunk));
    if (count == 0) {
        (void)close(stream->fd);
        stream->fd = -1;
        return 0;
    }
    if (count < 0)
        return (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) ? 0 : -1;

    size_t bytes = (size_t)count;
    size_t keep = stream->capacity - stream->retained;
    if (keep > bytes)
        keep = bytes;
    memcpy(stream->data + stream->retained, chunk, keep);
    stream->retained += keep;
    stream->observed += bytes;
    return stream->observed > stream->capacity ? 1 : 0;
}

static int emit_hex(const unsigned char *data, size_t length)
{
    const char digits[] = "0123456789abcdef";
    char chunk[8192];
    while (length != 0) {
        size_t count = length > sizeof(chunk) / 2 ? sizeof(chunk) / 2 : length;
        for (size_t index = 0; index < count; ++index) {
            chunk[index * 2] = digits[data[index] >> 4];
            chunk[index * 2 + 1] = digits[data[index] & 15];
        }
        if (fwrite(chunk, 2, count, stdout) != count)
            return -1;
        data += count;
        length -= count;
    }
    return 0;
}

static int emit_report(const char *reason, int status, const struct rusage *usage,
                       double elapsed, const struct stream *out, const struct stream *err)
{
    double cpu = (double)usage->ru_utime.tv_sec + (double)usage->ru_stime.tv_sec
        + ((double)usage->ru_utime.tv_usec + (double)usage->ru_stime.tv_usec) / 1000000.0;
    unsigned long long memory = (unsigned long long)usage->ru_maxrss * 1024ULL;
    if (printf("{\"launcher_version\":\"validator-launcher-v1\",\"reason\":\"%s\",\"exit_code\":", reason) < 0)
        return -1;
    if (WIFEXITED(status)) {
        if (printf("%d", WEXITSTATUS(status)) < 0)
            return -1;
    } else if (fputs("null", stdout) == EOF) {
        return -1;
    }
    if (fputs(",\"signal\":", stdout) == EOF)
        return -1;
    if (WIFSIGNALED(status)) {
        if (printf("%d", WTERMSIG(status)) < 0)
            return -1;
    } else if (fputs("null", stdout) == EOF) {
        return -1;
    }
    if (fputs(",\"stdout_hex\":\"", stdout) == EOF
            || emit_hex(out->data, out->retained) != 0
            || fputs("\",\"stderr_hex\":\"", stdout) == EOF
            || emit_hex(err->data, err->retained) != 0)
        return -1;
    if (printf("\",\"stdout_bytes\":%llu,\"stderr_bytes\":%llu,"
               "\"cpu_seconds\":%.6f,\"peak_memory_bytes\":%llu,"
               "\"worker_wall_seconds\":%.6f}\n",
               (unsigned long long)out->observed, (unsigned long long)err->observed,
               cpu, memory, elapsed) < 0)
        return -1;
    return fflush(stdout) == 0 ? 0 : -1;
}

int main(int argc, char **argv)
{
    int out_pipe[2] = {-1, -1};
    int err_pipe[2] = {-1, -1};
    int exec_pipe[2] = {-1, -1};
    unsigned char out_data[STDOUT_LIMIT];
    unsigned char err_data[STDERR_LIMIT];
    char **child_argv = NULL;
    char *const child_env[] = {
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        "PYTHONHASHSEED=0",
        NULL
    };
    pid_t child = -1;
    int status = 0;
    int reaped = 0;
    int failed = 0;
    const char *reason = NULL;
    struct rusage usage;
    memset(&usage, 0, sizeof(usage));

    if (argc < 1 || argc > 64)
        return BOOTSTRAP_FAILURE;
    child_argv = calloc((size_t)argc + 6, sizeof(*child_argv));
    if (child_argv == NULL)
        return BOOTSTRAP_FAILURE;
    child_argv[0] = "/usr/local/bin/python3";
    /* -I would ignore the fixed hash seed. execve supplies only this clean
       environment; these flags exclude user site packages and unsafe paths. */
    child_argv[1] = "-B";
    child_argv[2] = "-s";
    child_argv[3] = "-P";
    child_argv[4] = "-u";
    child_argv[5] = "/opt/validator/worker.py";
    for (int index = 1; index < argc; ++index)
        child_argv[index + 5] = argv[index];

    if (pipe2(out_pipe, O_CLOEXEC) != 0 || pipe2(err_pipe, O_CLOEXEC) != 0
            || pipe2(exec_pipe, O_CLOEXEC) != 0) {
        failed = 1;
        goto cleanup;
    }
    double started = monotonic_seconds();
    if (started < 0) {
        failed = 1;
        goto cleanup;
    }
    child = fork();
    if (child < 0) {
        failed = 1;
        goto cleanup;
    }
    if (child == 0) {
        if (setpgid(0, 0) != 0
                || dup2(out_pipe[1], STDOUT_FILENO) < 0
                || dup2(err_pipe[1], STDERR_FILENO) < 0)
            child_bootstrap_failed(exec_pipe[1]);
        /* Descriptor 3 reports only pre-exec failure and closes on exec. */
        if (dup3(exec_pipe[1], 3, O_CLOEXEC) < 0)
            child_bootstrap_failed(exec_pipe[1]);
        if (close_child_descriptors() != 0)
            child_bootstrap_failed(3);
        execve(child_argv[0], child_argv, child_env);
        child_bootstrap_failed(3);
    }

    /* The child repeats setpgid so it cannot execute before group creation. */
    if (setpgid(child, child) != 0 && errno != EACCES && errno != ESRCH) {
        failed = 1;
        goto cleanup;
    }
    (void)close(out_pipe[1]);
    out_pipe[1] = -1;
    (void)close(err_pipe[1]);
    err_pipe[1] = -1;
    (void)close(exec_pipe[1]);
    exec_pipe[1] = -1;
    (void)close(STDIN_FILENO);
    if (nonblocking(out_pipe[0]) != 0 || nonblocking(err_pipe[0]) != 0
            || nonblocking(exec_pipe[0]) != 0) {
        failed = 1;
        goto cleanup;
    }

    struct stream out = {out_pipe[0], out_data, STDOUT_LIMIT, 0, 0};
    struct stream err = {err_pipe[0], err_data, STDERR_LIMIT, 0, 0};
    /* stream now owns these descriptors. */
    out_pipe[0] = -1;
    err_pipe[0] = -1;
    double killed_at = -1.0;
    double ended = started;

    while (!reaped || out.fd >= 0 || err.fd >= 0 || exec_pipe[0] >= 0) {
        double now = monotonic_seconds();
        if (now < 0) {
            failed = 1;
            break;
        }
        if (!reaped) {
            pid_t result = wait4(child, &status, WNOHANG, &usage);
            if (result == child) {
                reaped = 1;
                /* Child lifetime observed by wait4, including Python startup.
                   Pipe draining and host/container overhead are separate. */
                ended = now;
            } else if (result < 0 && errno != EINTR) {
                failed = 1;
                break;
            }
        }
        /* Enforce completion and pipe closure, not merely child exit. */
        if (now - started >= WALL_SECONDS && killed_at < 0) {
            if (reason == NULL)
                reason = "timeout";
            terminate_worker(child);
            killed_at = now;
        }
        if (killed_at >= 0 && now - killed_at >= REAP_GRACE_SECONDS) {
            /* A stuck kernel task must not hold PID 1 open indefinitely. */
            if (!reaped || out.fd >= 0 || err.fd >= 0 || exec_pipe[0] >= 0)
                failed = 1;
            break;
        }

        struct pollfd ready[3] = {{out.fd, POLLIN, 0}, {err.fd, POLLIN, 0},
                                  {exec_pipe[0], POLLIN, 0}};
        double until = killed_at < 0 ? started + WALL_SECONDS : killed_at + REAP_GRACE_SECONDS;
        double remaining = until - now;
        int wait_ms = remaining > 0.02 ? 20 : remaining > 0 ? (int)(remaining * 1000) + 1 : 0;
        int result = poll(ready, 3, wait_ms);
        if (result < 0) {
            if (errno == EINTR)
                continue;
            failed = 1;
            break;
        }
        if (ready[2].revents & POLLNVAL) {
            failed = 1;
            break;
        }
        if (ready[2].revents & (POLLIN | POLLHUP | POLLERR)) {
            int exec_error;
            ssize_t count = read(exec_pipe[0], &exec_error, sizeof(exec_error));
            if (count == 0) {
                (void)close(exec_pipe[0]);
                exec_pipe[0] = -1;
            } else if (count > 0 || (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK)) {
                failed = 1;
                break;
            }
        }
        struct stream *streams[2] = {&out, &err};
        for (int index = 0; index < 2; ++index) {
            if (ready[index].revents & POLLNVAL) {
                failed = 1;
                break;
            }
            if (ready[index].revents & (POLLIN | POLLHUP | POLLERR)) {
                int drained = drain_once(streams[index]);
                if (drained < 0) {
                    failed = 1;
                    break;
                }
                if (drained > 0 && killed_at < 0) {
                    reason = index == 0 ? "output_limit" : "stderr_limit";
                    terminate_worker(child);
                    killed_at = monotonic_seconds();
                    if (killed_at < 0) {
                        failed = 1;
                        break;
                    }
                }
            }
        }
        if (failed)
            break;
    }

    if (out.fd >= 0)
        (void)close(out.fd);
    if (err.fd >= 0)
        (void)close(err.fd);
    if (!failed) {
        if (reason == NULL)
            reason = WIFSIGNALED(status) ? "process_terminated"
                : WIFEXITED(status) && WEXITSTATUS(status) == 0 ? "completed" : "worker_error";
        if (emit_report(reason, status, &usage, ended - started, &out, &err) != 0)
            failed = 1;
    }

cleanup:
    if (failed && child > 0 && !reaped)
        terminate_worker(child);
    if (out_pipe[0] >= 0)
        (void)close(out_pipe[0]);
    if (out_pipe[1] >= 0)
        (void)close(out_pipe[1]);
    if (err_pipe[0] >= 0)
        (void)close(err_pipe[0]);
    if (err_pipe[1] >= 0)
        (void)close(err_pipe[1]);
    if (exec_pipe[0] >= 0)
        (void)close(exec_pipe[0]);
    if (exec_pipe[1] >= 0)
        (void)close(exec_pipe[1]);
    free(child_argv);
    return failed ? BOOTSTRAP_FAILURE : 0;
}
