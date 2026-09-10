# Electronic Cats
# Makefile — local entry points for the packaging scripts under packaging/.
# See docs/PACKAGING_PLAN.md for the full multi-platform plan.

VERSION := $(shell tr -d '[:space:]' < VERSION)

.PHONY: help deb arch mac install uninstall version clean

help:
	@echo "bombercat packaging ($(VERSION))"
	@echo "  make deb        build bombercat-$(VERSION).deb (needs dpkg-deb)"
	@echo "  make arch       build packaging/bombercat-$(VERSION)-1-any.pkg.tar.zst (needs makepkg)"
	@echo "  make mac        build bombercat-$(VERSION).pkg (needs pyinstaller, macOS only)"
	@echo "  make install    pip install . into the active environment"
	@echo "  make uninstall  pip uninstall bombercat"
	@echo "  make version    print the current VERSION"
	@echo "  make clean      remove build artifacts"

deb:
	bash packaging/build_deb.sh $(VERSION)

arch:
	bash packaging/build_arch.sh $(VERSION)

mac:
	./build_mac.sh $(VERSION)

install:
	pip install .

uninstall:
	pip uninstall -y bombercat

version:
	@echo $(VERSION)

clean:
	rm -rf build dist pkg_root *.egg-info \
	    packaging/build_deb packaging/build_arch \
	    bombercat-*.deb packaging/bombercat-*.pkg.tar.zst bombercat-*.pkg bombercat-*.exe \
	    packaging/windows/bombercat_installer.rendered.iss
