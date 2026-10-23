/**
 * BomberCat MifareClassic - native parity harness.
 *
 * Compiles the firmware's MifareAccessBits / MifareBlock0 next to the host's
 * Python twins and compares their verdicts (Plan de Implementacion Firmware
 * MIFARE, Fase 4 / tarea 4.4).
 *
 * The harness is deliberately dumb: it reads one vector per line from stdin
 * and prints one canonical line per vector. The pytest that drives it
 * (bombercat-tools/tests/test_mifare_parity.py) builds the SAME canonical
 * line with the Python modules and asserts the two match - so a change in
 * either implementation that the other does not follow fails the suite.
 *
 * Protocol (one line in, one line out):
 *   access <6 hex>   -> A <valid> <12 permission names>
 *   block0 <32 hex>  -> B <uid> <bcc> <bccvalid> <sak> <atqa> <mfg>
 *                               <sak name>|<atqa name>
 *
 * The permission names are the host's own strings ("never", "key A",
 * "key B", "key A or B"), so the comparison is literal.
 *
 * Distributed as-is; no warranty is given.
 */
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

#include "MifareAccessBits.h"
#include "MifareBlock0.h"

namespace {

int hexNibble(char c) {
  if (c >= '0' && c <= '9') {
    return c - '0';
  }
  if (c >= 'A' && c <= 'F') {
    return c - 'A' + 10;
  }
  if (c >= 'a' && c <= 'f') {
    return c - 'a' + 10;
  }
  return -1;
}

bool parseHex(const std::string &hex, uint8_t *out, size_t expectedBytes) {
  if (hex.size() != expectedBytes * 2) {
    return false;
  }
  for (size_t i = 0; i < expectedBytes; i++) {
    const int hi = hexNibble(hex[i * 2]);
    const int lo = hexNibble(hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) {
      return false;
    }
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

std::string toHex(const uint8_t *data, size_t len) {
  static const char kDigits[] = "0123456789ABCDEF";
  std::string out;
  out.reserve(len * 2);
  for (size_t i = 0; i < len; i++) {
    out.push_back(kDigits[(data[i] >> 4) & 0x0F]);
    out.push_back(kDigits[data[i] & 0x0F]);
  }
  return out;
}

const char *permName(MifareAccessPerm perm) {
  // Space-free names: the canonical line is compared token by token, and the
  // host's own names ("key A", "key A or B") carry spaces.
  switch (perm) {
  case MIFARE_PERM_KEY_A:
    return "key_A";
  case MIFARE_PERM_KEY_B:
    return "key_B";
  case MIFARE_PERM_KEY_AB:
    return "key_AB";
  case MIFARE_PERM_NEVER:
  default:
    return "never";
  }
}

} // namespace

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) {
      continue;
    }
    const size_t space = line.find(' ');
    if (space == std::string::npos) {
      continue;
    }
    const std::string verb = line.substr(0, space);
    const std::string payload = line.substr(space + 1);

    if (verb == "access") {
      uint8_t ac[3] = {0, 0, 0};
      if (!parseHex(payload, ac, 3)) {
        std::printf("ERR bad-hex\n");
        continue;
      }
      MifareAccessBits bits;
      mifareParseAccessBits(ac, bits);
      std::printf("A %d", bits.valid ? 1 : 0);
      for (int b = 0; b < 3; b++) {
        std::printf(
            " %s|%s|%s|%s", permName(bits.blocks[b].read),
            permName(bits.blocks[b].write), permName(bits.blocks[b].increment),
            permName(bits.blocks[b].decrement));
      }
      std::printf(
          " %s|%s|%s|%s|%s\n", permName(bits.trailer.keyAWrite),
          permName(bits.trailer.acRead), permName(bits.trailer.acWrite),
          permName(bits.trailer.keyBRead), permName(bits.trailer.keyBWrite));
      continue;
    }

    if (verb == "block0") {
      uint8_t block[16];
      if (!parseHex(payload, block, 16)) {
        std::printf("ERR bad-hex\n");
        continue;
      }
      MifareBlock0 b0;
      mifareParseBlock0(block, b0);
      // The names carry spaces, so they are emitted space-free too.
      std::string sakName(mifareSakName(b0.sak));
      std::string atqaName(mifareAtqaName(b0.atqa));
      for (char &c : sakName) {
        if (c == ' ') {
          c = '_';
        }
      }
      for (char &c : atqaName) {
        if (c == ' ') {
          c = '_';
        }
      }
      std::printf("B %s %s %d %s %s %s %s|%s\n", toHex(b0.uid, 4).c_str(),
                  toHex(&b0.bcc, 1).c_str(), b0.bccValid ? 1 : 0,
                  toHex(&b0.sak, 1).c_str(), toHex(b0.atqa, 2).c_str(),
                  toHex(b0.manufacturer, 8).c_str(), sakName.c_str(),
                  atqaName.c_str());
      continue;
    }

    std::printf("ERR unknown-verb\n");
  }
  return 0;
}
