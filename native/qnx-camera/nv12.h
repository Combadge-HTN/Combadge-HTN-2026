#ifndef COMMBADGE_NV12_H
#define COMMBADGE_NV12_H
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Convert strided NV12 to packed planar 4:2:0, using integer downsampling.
 * Keeping this independent of camera/JPEG APIs makes the layout testable.
 */
static int unpack_nv12(const uint8_t *data, unsigned width, unsigned height,
    unsigned stride, int64_t uv_offset, int64_t uv_stride, unsigned max_dimension,
    uint8_t **result, unsigned *out_width, unsigned *out_height)
{
    if (!data || width < 2 || height < 2 || width > 8192 || height > 8192 ||
        width % 2 || height % 2 || stride < width || stride > 16384 ||
        uv_stride < width || uv_stride > 16384 ||
        uv_offset < (int64_t)stride * height || uv_offset > 128 * 1024 * 1024 ||
        max_dimension < 2 || max_dimension > 8192) return -1;
    unsigned longest = width > height ? width : height;
    unsigned step = (longest + max_dimension - 1) / max_dimension;
    unsigned w = (width / step) & ~1u, h = (height / step) & ~1u;
    if (!w || !h) return -1;
    size_t ysize = (size_t)w * h;
    uint8_t *out = malloc(ysize + ysize / 2);
    if (!out) return -1;
    for (unsigned y = 0; y < h; y++) {
        const uint8_t *row = data + (size_t)y * step * stride;
        for (unsigned x = 0; x < w; x++) out[(size_t)y * w + x] = row[x * step];
    }
    for (unsigned y = 0; y < h / 2; y++) {
        const uint8_t *row = data + uv_offset + (int64_t)y * step * uv_stride;
        for (unsigned x = 0; x < w / 2; x++) {
            size_t p = (size_t)y * (w / 2) + x;
            out[ysize + p] = row[2 * x * step];
            out[ysize + ysize / 4 + p] = row[2 * x * step + 1];
        }
    }
    *result = out;
    *out_width = w;
    *out_height = h;
    return 0;
}
#endif
