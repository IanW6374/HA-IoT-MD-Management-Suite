Browser USB dependencies are vendored so factory seeding does not execute code
from a CDN. Reproduce these files from the corresponding npm release archives:

- `esptool-js-0.7.0.js`: `esptool-js@0.7.0` / `bundle.js`, Apache-2.0.
- `spark-md5-3.0.2.js`: `spark-md5@3.0.2` / `spark-md5.min.js`, MIT.

Their upstream license texts are included alongside the JavaScript files.
The esptool-js bundle also includes pako (MIT/Zlib) and atob-lite (MIT); their
license texts are included here as well.
esptool-js uses MD5 solely to compare transferred bytes with flash contents;
factory Secure Boot signatures remain enforced by the device at first boot.
