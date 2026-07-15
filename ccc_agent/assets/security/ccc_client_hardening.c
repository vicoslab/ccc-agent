#define _GNU_SOURCE

#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <unistd.h>

/*
 * Loaded only into the initial Claude/Codex client and its MCP child.
 *
 * PR_SET_DUMPABLE=0 prevents same-UID tool descendants from reopening the
 * trusted client's descriptors through /proc/<pid>/fd.  CLOEXEC on newly
 * created pipes/socketpairs prevents those transport descriptors from leaking
 * through exec into Bash or other agent-launched programs.  Explicit child
 * stdio still works: posix_spawn/fork launchers dup2 the selected descriptors
 * onto 0/1/2, which clears close-on-exec on the destination descriptors.
 */

static int (*next_pipe)(int pipefd[2]);
static int (*next_pipe2)(int pipefd[2], int flags);
static int (*next_socketpair)(int domain, int type, int protocol, int sv[2]);
static int (*next_dup)(int oldfd);
static int (*next_dup3)(int oldfd, int newfd, int flags);

static void set_cloexec(int fd)
{
    int flags = fcntl(fd, F_GETFD);
    if (flags >= 0)
        (void)fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
}

__attribute__((constructor)) static void ccc_harden_client(void)
{
    const char *enabled = getenv("CCC_AGENT_HARDEN_CLIENT");
    if (enabled != NULL && enabled[0] == '1') {
        if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0)
            _exit(125);
    }
}

int pipe(int pipefd[2])
{
    int rc;
    if (next_pipe == NULL)
        next_pipe = dlsym(RTLD_NEXT, "pipe");
    if (next_pipe == NULL)
        return -1;
    rc = next_pipe(pipefd);
    if (rc == 0) {
        set_cloexec(pipefd[0]);
        set_cloexec(pipefd[1]);
    }
    return rc;
}

int pipe2(int pipefd[2], int flags)
{
    if (next_pipe2 == NULL)
        next_pipe2 = dlsym(RTLD_NEXT, "pipe2");
    if (next_pipe2 == NULL)
        return -1;
    return next_pipe2(pipefd, flags | O_CLOEXEC);
}

int socketpair(int domain, int type, int protocol, int sv[2])
{
    if (next_socketpair == NULL)
        next_socketpair = dlsym(RTLD_NEXT, "socketpair");
    if (next_socketpair == NULL)
        return -1;
    return next_socketpair(domain, type | SOCK_CLOEXEC, protocol, sv);
}

int dup(int oldfd)
{
    int fd;
    if (next_dup == NULL)
        next_dup = dlsym(RTLD_NEXT, "dup");
    if (next_dup == NULL)
        return -1;
    fd = next_dup(oldfd);
    if (fd >= 0)
        set_cloexec(fd);
    return fd;
}

int dup3(int oldfd, int newfd, int flags)
{
    if (next_dup3 == NULL)
        next_dup3 = dlsym(RTLD_NEXT, "dup3");
    if (next_dup3 == NULL)
        return -1;
    return next_dup3(oldfd, newfd, flags | O_CLOEXEC);
}
