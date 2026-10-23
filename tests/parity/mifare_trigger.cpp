// BomberCat - native harness for the firmware's standalone field trigger.
//
// Compiles MifareStandaloneTrigger.cpp (firmware/MifareClassic) with no
// Arduino and lets the test drive it: the button samples go in, the gesture
// verdicts come out. The Python side of the test holds the SPEC of the same
// behaviour, so the two are compared sample by sample.
//
// Protocol (one command per line on stdin):
//
//   poll <0|1> <now_ms>  -> "none" | "short" | "long"
//   arm                   -> "armed"
//   pending               -> "0" | "1"
//   take                  -> "0" | "1"   (reads AND clears the latch)
//   counts                -> "<presses> <long_presses>"
//   blink <count> <on_ms> <off_ms>      (arm the LED pattern)
//   led <now_ms>          -> "0" | "1"  (the LED state at that instant)

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "MifareStandaloneTrigger.h"

int main() {
  MifareStandaloneTrigger trig;
  MifareLedBlink led;
  trig.begin(0);
  led.begin();

  char line[256];
  while (fgets(line, sizeof(line), stdin) != nullptr) {
    char cmd[32] = {0};
    char a[64] = {0};
    char b[64] = {0};
    char c[64] = {0};
    const int fields = sscanf(line, "%31s %63s %63s %63s", cmd, a, b, c);
    if (fields < 1) {
      continue;
    }
    if (strcmp(cmd, "poll") == 0 && fields >= 3) {
      const bool pressed = (a[0] == '1');
      const uint32_t now = (uint32_t)strtoul(b, nullptr, 10);
      const int event = trig.poll(pressed, now);
      const char *name = "none";
      if (event == MIFARE_TRIGGER_SHORT) {
        name = "short";
      } else if (event == MIFARE_TRIGGER_LONG) {
        name = "long";
      }
      printf("%s\n", name);
    } else if (strcmp(cmd, "arm") == 0) {
      trig.armOneShot();
      printf("armed\n");
    } else if (strcmp(cmd, "pending") == 0) {
      printf("%d\n", trig.oneShotPending() ? 1 : 0);
    } else if (strcmp(cmd, "take") == 0) {
      printf("%d\n", trig.takeOneShot() ? 1 : 0);
    } else if (strcmp(cmd, "counts") == 0) {
      printf("%u %u\n", (unsigned)trig.presses(), (unsigned)trig.longPresses());
    } else if (strcmp(cmd, "blink") == 0 && fields >= 4) {
      const uint8_t count = (uint8_t)strtoul(a, nullptr, 10);
      const uint16_t onMs = (uint16_t)strtoul(b, nullptr, 10);
      const uint16_t offMs = (uint16_t)strtoul(c, nullptr, 10);
      led.blinkTimes(count, onMs, offMs);
      printf("blinking\n");
    } else if (strcmp(cmd, "led") == 0 && fields >= 2) {
      const uint32_t now = (uint32_t)strtoul(a, nullptr, 10);
      printf("%d\n", led.update(now) ? 1 : 0);
    }
  }
  return 0;
}
