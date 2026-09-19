CC = cc
CFLAGS = -O2 -std=c11 -Wall -Wextra -Werror -D_QNX_SOURCE

all: combadge-camera

combadge-camera: capture.c camera_compat.h nv12.h
	$(CC) $(CFLAGS) -o $@ capture.c -lcamapi -ldl -pthread

test-nv12: test_nv12.c nv12.h
	$(CC) $(CFLAGS) -o $@ test_nv12.c

test: test-nv12
	./test-nv12

.PHONY: all test
