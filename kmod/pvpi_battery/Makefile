# Out-of-tree build of the pvpi_battery module: `make`, or through DKMS (see docs/System-battery.md).
obj-m := pvpi_battery.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
