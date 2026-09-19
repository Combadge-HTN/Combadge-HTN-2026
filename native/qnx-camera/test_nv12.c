#include "nv12.h"
#include <assert.h>
#include <stdio.h>

int main(void) {
    /* Separate Y/UV strides and a gap before UV catch assumptions about
     * contiguous camera buffers. Padding must never become image content.
     */
    uint8_t frame[48];
    memset(frame, 255, sizeof(frame));
    for (unsigned y = 0; y < 4; y++)
        for (unsigned x = 0; x < 4; x++) frame[y * 6 + x] = y * 4 + x;
    const uint8_t uv[] = {40, 50, 41, 51, 42, 52, 43, 53};
    memcpy(frame + 28, uv, 4);
    memcpy(frame + 36, uv + 4, 4);
    uint8_t *out = NULL;
    unsigned width = 0, height = 0;
    assert(!unpack_nv12(frame, 4, 4, 6, 28, 8, 4, &out, &width, &height));
    assert(width == 4 && height == 4);
    for (unsigned i = 0; i < 16; i++) assert(out[i] == i);
    for (unsigned i = 0; i < 4; i++) {
        assert(out[16 + i] == 40 + i);
        assert(out[20 + i] == 50 + i);
    }
    free(out);
    assert(!unpack_nv12(frame, 4, 4, 6, 28, 8, 2, &out, &width, &height));
    const uint8_t reduced[] = {0, 2, 8, 10, 40, 50};
    assert(width == 2 && height == 2);
    assert(!memcmp(out, reduced, sizeof(reduced)));
    free(out);
    assert(unpack_nv12(frame, 3, 4, 6, 28, 8, 4, &out, &width, &height));
    assert(unpack_nv12(frame, 4, 4, 3, 28, 8, 4, &out, &width, &height));
    assert(unpack_nv12(frame, 4, 4, 6, 20, 8, 4, &out, &width, &height));
    assert(unpack_nv12(frame, 4, 4, 6, 28, 3, 4, &out, &width, &height));
    assert(unpack_nv12(frame, 4, 4, 6, 28, -1, 4, &out, &width, &height));
    assert(unpack_nv12(NULL, 4, 4, 6, 28, 8, 4, &out, &width, &height));
    puts("NV12 conversion tests passed");
    return 0;
}
