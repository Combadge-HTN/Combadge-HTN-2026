/* Minimal ABI declarations for the QNX 8 Pi 5 quick-start image.
 * Prefer the matching Sensor Framework SDK header when it is installed.
 * This fallback was checked against live buffers on the target: 104 bytes,
 * framebuf at offset 16, NV12 descriptor at offset 72. Unknown layouts fail.
 * The reserved bytes are metadata we neither interpret nor modify.
 */
#ifndef COMMBADGE_CAMERA_COMPAT_H
#define COMMBADGE_CAMERA_COMPAT_H
#include <stddef.h>
#include <stdint.h>

#if __has_include(<camera/camera_api.h>)
#include <camera/camera_api.h>
#else
#define COMMBADGE_CAMERA_FIXED_ABI 1
typedef int32_t camera_handle_t;
typedef struct {
    uint32_t height, width, stride;
    int64_t uv_offset, uv_stride;
} camera_frame_nv12_t;
typedef struct {
    int32_t frametype;
    uint64_t framesize;
    uint8_t *framebuf;
    uint8_t reserved[48];
    union { camera_frame_nv12_t nv12; } framedesc;
} camera_buffer_t;
#define CAMERA_FRAMETYPE_NV12 1
#define CAMERA_MODE_RO 5
_Static_assert(sizeof(camera_buffer_t) == 104, "Requires QNX 8 64-bit buffer ABI");
_Static_assert(offsetof(camera_buffer_t, framedesc) == 72, "Unexpected descriptor offset");
extern int camera_open(int unit, uint32_t mode, camera_handle_t *handle);
extern int camera_close(camera_handle_t handle);
extern int camera_start_viewfinder(camera_handle_t handle,
    void (*callback)(camera_handle_t, camera_buffer_t *, void *),
    void (*status_callback)(camera_handle_t, int, uint16_t, void *), void *arg);
extern int camera_stop_viewfinder(camera_handle_t handle);
#endif
#endif
