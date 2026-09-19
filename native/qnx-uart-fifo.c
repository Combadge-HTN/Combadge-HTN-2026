/* Prepare only the idle Pi 5 Bluetooth UART FIFO required by Dan's launcher.
 * GPIO/board/ownership checks are performed by start_pi.py before this runs.
 * No baud, interrupt, DMA, clock, or pin configuration is changed here.
 */
#define _QNX_SOURCE 1
#include <sys/mman.h>
#include <hw/inout.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv) {
    int prepare = argc == 2 && !strcmp(argv[1], "prepare");
    int restore = argc == 2 && !strcmp(argv[1], "restore");
    if (!prepare && !restore) return 2;
    void *p = mmap_device_memory(NULL, 0x20,
        PROT_READ | PROT_WRITE | PROT_NOCACHE, 0, UINT64_C(0x107d50c000));
    if (p == MAP_FAILED) { perror("Bluetooth UART mapping"); return 1; }
    uintptr_t r = (uintptr_t)p;
    unsigned fifo = in32(r + 8) & 0xc0;
    if (in32(r + 12) != 0 || in32(r + 16) != 0 || in32(r + 4) != 0 ||
        (in32(r + 20) & 0x60) != 0x60 || (fifo != 0 && fifo != 0xc0)) {
        fputs("Bluetooth UART is not empty and idle; refusing to change it\n", stderr);
        munmap_device_memory(p, 0x20);
        return 1;
    }
    /* With the radio powered down and its pins idle, discard bounded stale
     * receive bytes left by the previous session's controller shutdown. */
    unsigned drained = 0;
    while ((in32(r + 20) & 1) && drained < 256) {
        (void)in32(r);
        drained++;
    }
    if (in32(r + 20) & 1) {
        fputs("Bluetooth UART receive buffer did not become idle\n", stderr);
        munmap_device_memory(p, 0x20);
        return 1;
    }
    if (prepare && fifo == 0xc0) {
        puts("already-enabled");
        munmap_device_memory(p, 0x20);
        return 0;
    }
    /* On restore the radio is off and its process has exited. Residual RX
     * bytes may be discarded while restoring the original disabled FIFO. */
    out32(r + 8, prepare ? 1 : 0);
    unsigned after = in32(r + 8) & 0xc0;
    if (after != (prepare ? 0xc0 : 0)) {
        fputs("Bluetooth FIFO readback failed\n", stderr);
        munmap_device_memory(p, 0x20);
        return 1;
    }
    puts(prepare ? "enabled" : "restored");
    munmap_device_memory(p, 0x20);
    return 0;
}
