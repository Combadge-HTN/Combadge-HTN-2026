/* Capture one NV12 camera frame as a JPEG for Combadge's SnapshotCapture.
 * Build on the target with make -C native/qnx-camera.
 */
#include "camera_compat.h"
#include "nv12.h"
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted;
static void stop_requested(int signal_number) { (void)signal_number; interrupted = 1; }
static double monotonic_seconds(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + t.tv_nsec / 1e9;
}

struct capture {
    pthread_mutex_t lock;
    bool done;
    const char *error;
    double ready_at;
    unsigned max_dimension, width, height;
    uint8_t *planes;
};

static void receive_frame(camera_handle_t handle, camera_buffer_t *buffer, void *arg) {
    (void)handle;
    struct capture *state = arg;
    pthread_mutex_lock(&state->lock);
    if (state->done || interrupted || monotonic_seconds() < state->ready_at) {
        pthread_mutex_unlock(&state->lock);
        return;
    }
    if (!buffer) state->error = "Camera returned a null buffer";
#ifdef COMMBADGE_CAMERA_FIXED_ABI
    else if (buffer->framesize != sizeof(camera_buffer_t))
        state->error = "Unknown camera buffer ABI; rebuild with matching QNX camera SDK headers";
#endif
    else if (buffer->frametype != CAMERA_FRAMETYPE_NV12)
        state->error = "Camera is not configured for NV12; choose the physical IMX708 camera unit";
    else {
        camera_frame_nv12_t *d = &buffer->framedesc.nv12;
        if (unpack_nv12(buffer->framebuf, d->width, d->height, d->stride,
                d->uv_offset, d->uv_stride, state->max_dimension,
                &state->planes, &state->width, &state->height))
            state->error = "Invalid NV12 geometry or insufficient memory";
    }
    state->done = true;
    pthread_mutex_unlock(&state->lock);
}

/* TurboJPEG's stable C API. Loading it dynamically avoids requiring its
 * development headers or a linker symlink on the quick-start image.
 */
struct encoder {
    void *library;
    void *(*init)(void);
    int (*compress)(void *, const unsigned char **, int, const int *, int, int,
                    unsigned char **, unsigned long *, int, int);
    void (*release)(unsigned char *);
    int (*destroy)(void *);
    char *(*error)(void);
};

static int load_encoder(struct encoder *e) {
    e->library = dlopen("libturbojpeg.so.0", RTLD_NOW | RTLD_LOCAL);
    if (!e->library) { fprintf(stderr, "JPEG library: %s\n", dlerror()); return -1; }
#define LOAD(field, symbol) do { \
    *(void **)(&e->field) = dlsym(e->library, symbol); \
    if (!e->field) { fprintf(stderr, "JPEG library missing %s\n", symbol); return -1; } \
} while (0)
    LOAD(init, "tjInitCompress");
    LOAD(compress, "tjCompressFromYUVPlanes");
    LOAD(release, "tjFree");
    LOAD(destroy, "tjDestroy");
    LOAD(error, "tjGetErrorStr");
#undef LOAD
    return 0;
}

static int encode(struct encoder *e, struct capture *state,
                  unsigned char **jpeg, unsigned long *size) {
    void *handle = e->init();
    if (!handle) { fprintf(stderr, "JPEG initialization failed\n"); return -1; }
    size_t pixels = (size_t)state->width * state->height;
    const unsigned char *planes[] = {state->planes, state->planes + pixels,
                                    state->planes + pixels + pixels / 4};
    int strides[] = {(int)state->width, (int)state->width / 2, (int)state->width / 2};
    int result = -1;
    for (int quality = 85; quality >= 25 && !interrupted; quality -= 15) {
        if (e->compress(handle, planes, (int)state->width, strides,
                (int)state->height, 2 /* TJSAMP_420 */, jpeg, size, quality, 0)) {
            fprintf(stderr, "JPEG encoding: %s\n", e->error());
            break;
        }
        if (*size <= 256 * 1024) { result = 0; break; }
        e->release(*jpeg);
        *jpeg = NULL;
        *size = 0;
    }
    e->destroy(handle);
    if (result) fprintf(stderr, "Could not produce a JPEG under 256 KiB; lower --max-dimension\n");
    return result;
}

static int positive_number(const char *text, unsigned max, unsigned *value) {
    char *end;
    errno = 0;
    unsigned long n = strtoul(text, &end, 10);
    if (errno || !*text || *end || !n || n > max) return -1;
    *value = (unsigned)n;
    return 0;
}

static void usage(void) {
    fprintf(stderr, "Usage: combadge-camera --output-dir DIR [--unit 4] "
        "[--timeout 10] [--max-dimension 1280]\n"
        "Captures one fresh NV12 frame after a 1-second warmup as snapshot.jpg.\n");
}

int main(int argc, char **argv) {
    const char *directory = NULL;
    unsigned unit = 4, timeout = 10, max_dimension = 1280;
    const struct option options[] = {
        {"output-dir", required_argument, NULL, 'o'},
        {"unit", required_argument, NULL, 'u'},
        {"timeout", required_argument, NULL, 't'},
        {"max-dimension", required_argument, NULL, 'm'},
        {"help", no_argument, NULL, 'h'}, {NULL, 0, NULL, 0}
    };
    int option;
    while ((option = getopt_long(argc, argv, "o:u:t:m:h", options, NULL)) != -1) {
        switch (option) {
        case 'o': directory = optarg; break;
        case 'u': if (positive_number(optarg, 64, &unit)) goto invalid; break;
        case 't': if (positive_number(optarg, 20, &timeout)) goto invalid; break;
        case 'm': if (positive_number(optarg, 4096, &max_dimension)) goto invalid; break;
        case 'h': usage(); return 0;
        default: goto invalid;
        }
    }
    if (!directory || optind != argc || timeout < 2 || max_dimension < 2) goto invalid;

    int result = 1, directory_fd = -1, output_fd = -1;
    camera_handle_t handle = -1;
    bool opened = false, started = false, created = false;
    unsigned char *jpeg = NULL;
    unsigned long jpeg_size = 0;
    struct encoder encoder = {0};
    struct capture state = {.lock = PTHREAD_MUTEX_INITIALIZER,
                            .max_dimension = max_dimension};
    struct sigaction action = {0};
    action.sa_handler = stop_requested;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    directory_fd = open(directory, O_RDONLY | O_DIRECTORY);
    if (directory_fd < 0) { perror("Output directory"); goto cleanup; }
    if (load_encoder(&encoder)) goto cleanup;
    int rc = camera_open((int)unit, CAMERA_MODE_RO, &handle);
    if (rc) { fprintf(stderr, "Cannot open camera unit %u: error %d\n", unit, rc); goto cleanup; }
    opened = true;
    double began = monotonic_seconds();
    state.ready_at = began + 1.0;
    rc = camera_start_viewfinder(handle, receive_frame, NULL, &state);
    if (rc) { fprintf(stderr, "Cannot start camera: error %d\n", rc); goto cleanup; }
    started = true;
    while (!interrupted && monotonic_seconds() - began < timeout) {
        pthread_mutex_lock(&state.lock);
        bool done = state.done;
        pthread_mutex_unlock(&state.lock);
        if (done) break;
        struct timespec delay = {.tv_nsec = 10000000};
        nanosleep(&delay, NULL);
    }
    rc = camera_stop_viewfinder(handle);
    if (rc) { fprintf(stderr, "Cannot stop camera: error %d\n", rc); goto cleanup; }
    started = false;
    if (interrupted) goto cleanup;
    if (!state.done) { fprintf(stderr, "Timed out waiting for a camera frame\n"); goto cleanup; }
    if (state.error) { fprintf(stderr, "%s\n", state.error); goto cleanup; }
    if (encode(&encoder, &state, &jpeg, &jpeg_size) || interrupted) goto cleanup;
    /* Never overwrite an existing snapshot, including a symlink. */
    output_fd = openat(directory_fd, "snapshot.jpg", O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (output_fd < 0) { perror("snapshot.jpg"); goto cleanup; }
    created = true;
    for (size_t offset = 0; offset < jpeg_size;) {
        if (interrupted) goto cleanup;
        ssize_t n = write(output_fd, jpeg + offset, jpeg_size - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { perror("Writing snapshot"); goto cleanup; }
        offset += (size_t)n;
    }
    if (close(output_fd)) { output_fd = -1; perror("Closing snapshot"); goto cleanup; }
    output_fd = -1;
    fprintf(stderr, "Captured camera %u: %ux%u JPEG, %lu bytes\n",
            unit, state.width, state.height, jpeg_size);
    result = 0;
cleanup:
    if (started) camera_stop_viewfinder(handle);
    if (opened && camera_close(handle)) {
        /* Do not free callback state if the driver could still be using it.
         * This one-shot process can let the OS reclaim its resources instead.
         */
        fprintf(stderr, "Cannot close camera\n");
        if (result && created) unlinkat(directory_fd, "snapshot.jpg", 0);
        fflush(stderr);
        _Exit(1);
    }
    if (output_fd >= 0) close(output_fd);
    if (result && created) unlinkat(directory_fd, "snapshot.jpg", 0);
    if (directory_fd >= 0) close(directory_fd);
    if (jpeg && encoder.release) encoder.release(jpeg);
    if (encoder.library) dlclose(encoder.library);
    free(state.planes);
    pthread_mutex_destroy(&state.lock);
    return interrupted ? 130 : result;
invalid:
    usage();
    return 2;
}
