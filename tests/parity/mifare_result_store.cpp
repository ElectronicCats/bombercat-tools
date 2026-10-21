// BomberCat - native harness for the firmware's standalone result store.
//
// Compiles MifareResultStore.cpp (firmware/MifareClassic) with no Arduino and
// lets the test drive it: the result to store goes in, the ring's behaviour
// comes out. The Python side of the test holds the SPEC of the same
// behaviour, so the two are compared step by step.
//
// The flash is a MAP here (the firmware's is ConfigStore's TDBStore), and
// every write is counted, so the test can check the wear budget: one write
// per stored card.
//
// Protocol (one command per line on stdin):
//
//   build <uid> <sectors> <read> <failed> <ts>   -> fill the record to store
//   sector <n> <status> <keytype> <hex64>        -> one sector of that record
//   store                                        -> "stored <slot> <seq>" | "failed"
//   scan                                         -> "<count> <next_slot> <next_seq>"
//   list                                         -> "<i> <seq> <slot> <sectors> <read>
//                                                     <failed> <uid>" per result
//   load <i>                                     -> "<n> <status> <keytype> <hex64>"
//                                                    per sector of that result
//   clear                                        -> "cleared"
//   writes                                       -> the number of blob writes so far
//   puts                                         -> the number of blobs in the store

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "MifareResultStore.h"

namespace {

// The fake flash: a map of blobs, and a count of the writes.
std::map<std::string, std::vector<uint8_t>> g_store;
int g_writes = 0;

bool blobRead(const char *key, void *buf, size_t size) {
  auto it = g_store.find(key);
  if (it == g_store.end() || it->second.size() != size) {
    return false;
  }
  memcpy(buf, it->second.data(), size);
  return true;
}

bool blobWrite(const char *key, const void *buf, size_t size) {
  g_writes++;
  const uint8_t *bytes = static_cast<const uint8_t *>(buf);
  g_store[key] = std::vector<uint8_t>(bytes, bytes + size);
  return true;
}

bool blobClear(const char *key) {
  g_store.erase(key);
  return true;
}

std::string hexOf(const uint8_t *data, size_t len) {
  static const char *digits = "0123456789ABCDEF";
  std::string out;
  for (size_t i = 0; i < len; i++) {
    out.push_back(digits[data[i] >> 4]);
    out.push_back(digits[data[i] & 0x0F]);
  }
  return out;
}

int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

} // namespace

int main() {
  MifareResultStore store;
  MifareResultBlob blob;
  blob.read = blobRead;
  blob.write = blobWrite;
  blob.clear = blobClear;
  store.setBlob(blob);

  // The record the caller builds (the firmware keeps its own buffer, so the
  // harness does the same).
  static MifareResultRecord record;

  char line[4096];
  while (fgets(line, sizeof(line), stdin) != nullptr) {
    char cmd[32] = {0};
    char a[64] = {0};
    char b[64] = {0};
    char c[64] = {0};
    char d[256] = {0};
    char e[64] = {0};
    const int fields =
        sscanf(line, "%31s %63s %63s %63s %255s %63s", cmd, a, b, c, d, e);
    if (fields < 1) {
      continue;
    }

    if (strcmp(cmd, "build") == 0 && fields >= 6) {
      mifareResultInit(record);
      snprintf(record.uid, sizeof(record.uid), "%.8s", a);
      record.sectors = (uint8_t)strtoul(b, nullptr, 10);
      record.read = (uint8_t)strtoul(c, nullptr, 10);
      record.failed = (uint8_t)strtoul(d, nullptr, 10);
      record.tsMs = (uint32_t)strtoul(e, nullptr, 10);
    } else if (strcmp(cmd, "sector") == 0 && fields >= 5) {
      const uint8_t n = (uint8_t)strtoul(a, nullptr, 10);
      const uint8_t status = (uint8_t)strtoul(b, nullptr, 10);
      const char keyType = c[0] ? c[0] : '-';
      uint8_t data[MIFARE_RESULT_SECTOR_BYTES];
      memset(data, 0, sizeof(data));
      const char *hex = d;
      if (strlen(hex) >= 2 * MIFARE_RESULT_SECTOR_BYTES) {
        for (size_t i = 0; i < MIFARE_RESULT_SECTOR_BYTES; i++) {
          const int hi = hexNibble(hex[2 * i]);
          const int lo = hexNibble(hex[2 * i + 1]);
          data[i] = (uint8_t)((hi < 0 ? 0 : hi) << 4 | (lo < 0 ? 0 : lo));
        }
      }
      mifareResultSetSector(record, n, status, keyType, data);
    } else if (strcmp(cmd, "store") == 0) {
      if (store.store(record)) {
        printf("stored %u %u\n", (unsigned)record.slot, (unsigned)record.seq);
      } else {
        printf("failed\n");
      }
    } else if (strcmp(cmd, "scan") == 0) {
      const uint8_t count = store.scan();
      printf("%u %u %u\n", (unsigned)count, (unsigned)store.nextSlot(),
             (unsigned)store.nextSeq());
    } else if (strcmp(cmd, "list") == 0) {
      MifareResultMeta meta[MIFARE_RESULT_SLOTS];
      const uint8_t n = store.list(meta, MIFARE_RESULT_SLOTS);
      for (uint8_t i = 0; i < n; i++) {
        printf("%u %u %u %u %u %u %s\n", (unsigned)i, (unsigned)meta[i].seq,
               (unsigned)meta[i].slot, (unsigned)meta[i].sectors,
               (unsigned)meta[i].read, (unsigned)meta[i].failed, meta[i].uid);
      }
      printf("--\n");
    } else if (strcmp(cmd, "load") == 0 && fields >= 2) {
      const uint8_t index = (uint8_t)strtoul(a, nullptr, 10);
      const MifareResultRecord *rec = store.load(index);
      if (rec == nullptr) {
        printf("none\n");
      } else {
        for (uint8_t s = 0; s < rec->sectors; s++) {
          const std::string hex = hexOf(rec->data[s], MIFARE_RESULT_SECTOR_BYTES);
          printf("%u %u %c %s\n", (unsigned)s, (unsigned)rec->status[s],
                 (char)rec->keyType[s], hex.c_str());
        }
        printf("--\n");
      }
    } else if (strcmp(cmd, "clear") == 0) {
      printf("%s\n", store.clear() ? "cleared" : "failed");
    } else if (strcmp(cmd, "writes") == 0) {
      printf("%d\n", g_writes);
    } else if (strcmp(cmd, "puts") == 0) {
      printf("%zu\n", g_store.size());
    }
  }
  return 0;
}
