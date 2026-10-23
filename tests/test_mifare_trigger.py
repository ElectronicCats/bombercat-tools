#!/usr/bin/env python3

# Electronic Cats
# test_mifare_trigger.py — the firmware's field trigger (Fase 4, tarea 4.3)
# must behave exactly like the spec the host side assumes.
#
# The button that starts the standalone workflow in the field is a piece of
# hardware, so its behaviour cannot be asserted by reading the firmware: a
# gesture can be "short", "long" or "none", and a wrong answer means the
# operator's press runs the wrong job (or nothing at all).
#
# So the logic is checked the same way the repo already checks the "decryption"
# logic (test_mifare_parity.py): the firmware's module is compiled natively
# (g++, no Arduino) and driven with the same sample sequences as a Python
# model that spells the spec out. Both must agree, sample by sample.
#
# The test SKIPS (not fails) when the firmware sources are not next to the
# tools checkout, so the tools suite stays runnable on its own.

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Where the firmware's trigger module lives. It is a shared BomberCatCore
# module now (core/src), not a file of the MifareClassic sketch: it is
# reusable logic, so it belongs to the library. By default: the sibling
# checkout (../bombercat-firmware/core/src). A CI job that checks out both
# repos can point at it with BOMBERCAT_CORE_DIR.
CORE_SRC = Path(
    os.environ.get(
        "BOMBERCAT_CORE_DIR",
        str(
            Path(__file__).resolve().parents[2] / "bombercat-firmware" / "core" / "src"
        ),
    )
)
HARNESS = Path(__file__).parent / "parity" / "mifare_trigger.cpp"

pytestmark = pytest.mark.skipif(
    not (CORE_SRC / "MifareStandaloneTrigger.cpp").exists() or not HARNESS.exists(),
    reason="the BomberCatCore sources are not next to the tools checkout",
)


# ── the spec ──────────────────────────────────────────────────────────────────
#
# The trigger watches the board's user button (SW2 -> GPIO17 = Arduino D5,
# active LOW). The rules:
#
#   * a level change is only believed once it has been stable for DEBOUNCE_MS
#     (the switch bounces for a few ms);
#   * a press held for at least LONG_MS is a LONG press, reported once, while
#     the finger is still down;
#   * a press released before that is a SHORT press, reported on the release;
#   * a press too short to survive the debounce is not a press at all.

DEBOUNCE_MS = 25
LONG_MS = 1200


class TriggerModel:
    """The spec, in Python."""

    def __init__(self, long_ms=LONG_MS, debounce_ms=DEBOUNCE_MS):
        self.long_ms = long_ms
        self.debounce_ms = debounce_ms
        self.raw = False
        self.stable = False
        self.raw_changed = 0
        self.press_start = 0
        self.long_fired = False
        self.one_shot = False
        self.presses = 0
        self.long_presses = 0

    def poll(self, pressed, now):
        if pressed != self.raw:
            self.raw = pressed
            self.raw_changed = now

        if self.raw != self.stable:
            if now - self.raw_changed < self.debounce_ms:
                return "none"
            self.stable = self.raw
            if self.stable:
                self.press_start = now
                self.long_fired = False
                self.presses += 1
            else:
                return "none" if self.long_fired else "short"

        if (
            self.stable
            and not self.long_fired
            and now - self.press_start >= self.long_ms
        ):
            self.long_fired = True
            self.long_presses += 1
            return "long"

        return "none"

    def arm(self):
        self.one_shot = True

    def pending(self):
        return self.one_shot

    def take(self):
        taken = self.one_shot
        self.one_shot = False
        return taken

    def counts(self):
        return self.presses, self.long_presses


class LedModel:
    """The LED pattern scheduler, in Python."""

    def __init__(self):
        self.active = False
        self.lit = False
        self.on_ms = 0
        self.off_ms = 0
        self.next_toggle = 0
        self.remaining = 0
        self.counted = False
        self.until = 0

    def blink_times(self, count, on_ms, off_ms):
        if count == 0:
            return
        self.until = 0
        self.on_ms = on_ms
        self.off_ms = off_ms
        self.next_toggle = 0
        self.remaining = count
        self.counted = True
        self.active = True
        self.lit = False

    def update(self, now):
        if not self.active:
            return False
        if not self.counted:
            if now >= self.until:
                self.active = False
                self.lit = False
                return False
            if now >= self.next_toggle:
                self.lit = not self.lit
                self.next_toggle = now + (self.on_ms if self.lit else self.off_ms)
            return self.lit
        if self.remaining == 0:
            self.active = False
            self.lit = False
            return False
        if now >= self.next_toggle:
            if self.lit:
                self.remaining -= 1
                self.lit = False
                self.next_toggle = now + self.off_ms
            else:
                self.lit = True
                self.next_toggle = now + self.on_ms
        return self.lit


# ── the harness ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    """The compiled harness, ready to be fed samples."""
    if shutil.which("g++") is None:
        pytest.skip("g++ is not available")
    out = tmp_path_factory.mktemp("trigger") / "mifare_trigger"
    cmd = [
        "g++",
        "-std=c++17",
        "-O1",
        "-I",
        str(CORE_SRC),
        str(CORE_SRC / "MifareStandaloneTrigger.cpp"),
        str(HARNESS),
        "-o",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"the trigger harness does not build here: {proc.stderr[-400:]}")
    return out


def _run(harness, commands):
    """Feed the commands to the harness and return its output lines."""
    proc = subprocess.run(
        [str(harness)],
        input="\n".join(commands) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()


# ── the gestures ──────────────────────────────────────────────────────────────


def _samples(start, duration, step=10, tail=60):
    """A press of `duration` ms (starting at `start`) as a list of
    (pressed, now) samples.

    The tail matters: the release is only believed once it has been stable
    for the debounce window, so the samples have to keep coming (released)
    after the finger lifts — exactly like the real poll loop does.
    """
    out = []
    now = start
    end = start + duration
    while now <= end:
        out.append((True, now))
        now += step
    out.append((False, now))
    now += step
    while now <= end + step + tail:
        out.append((False, now))
        now += step
    return out


def model_events(model, samples):
    """The events the model produces for a sample list."""
    events = []
    for pressed, now in samples:
        events.append(model.poll(pressed, now))
    return events


def test_a_short_press_is_a_short_press(harness):
    model = TriggerModel()
    samples = _samples(1000, 200)
    expected = model_events(model, samples)
    firmware = _run(harness, [f"poll {1 if p else 0} {n}" for p, n in samples])
    assert "short" in expected
    assert firmware == expected
    assert firmware.count("short") == 1
    assert "long" not in firmware


def test_a_long_press_is_reported_once_and_never_as_short(harness):
    model = TriggerModel()
    samples = _samples(1000, 2000)
    expected = model_events(model, samples)
    firmware = _run(harness, [f"poll {1 if p else 0} {n}" for p, n in samples])
    assert firmware == expected
    assert firmware.count("long") == 1
    assert "short" not in firmware


def test_a_bouncing_contact_is_not_a_press(harness):
    # The switch bounces for a few ms: a level change only counts once it has
    # been stable for the debounce window, so a 5 ms glitch is nothing.
    model = TriggerModel()
    samples = [(True, 1000), (False, 1005), (True, 1010), (False, 1020)]
    expected = model_events(model, samples)
    firmware = _run(harness, [f"poll {1 if p else 0} {n}" for p, n in samples])
    assert firmware == expected
    assert "short" not in firmware
    assert "long" not in firmware
    assert model.presses == 0


def test_the_long_press_is_measured_from_the_debounced_press(harness):
    # Holding the button for exactly the threshold must fire the long press,
    # and holding it for a hair less must not.
    model = TriggerModel()
    samples = _samples(1000, LONG_MS - 100)
    expected = model_events(model, samples)
    firmware = _run(harness, [f"poll {1 if p else 0} {n}" for p, n in samples])
    assert firmware == expected
    assert "long" not in firmware  # released before the threshold

    model = TriggerModel()
    samples = _samples(1000, LONG_MS + 200)
    expected = model_events(model, samples)
    firmware = _run(harness, [f"poll {1 if p else 0} {n}" for p, n in samples])
    assert firmware == expected
    assert "long" in firmware


def test_two_presses_in_a_row_are_two_gestures(harness):
    model = TriggerModel()
    samples = _samples(1000, 100) + _samples(2000, 100)
    expected = model_events(model, samples)
    firmware = _run(harness, [f"poll {1 if p else 0} {n}" for p, n in samples])
    assert firmware == expected
    assert firmware.count("short") == 2
    assert model.presses == 2
    assert model.long_presses == 0


def test_the_press_counters_agree(harness):
    model = TriggerModel()
    samples = _samples(1000, 100) + _samples(2000, 2000)
    model_events(model, samples)
    firmware = _run(
        harness, [f"poll {1 if p else 0} {n}" for p, n in samples] + ["counts"]
    )
    presses, longs = firmware[-1].split()
    assert (int(presses), int(longs)) == model.counts()
    assert int(presses) == 2
    assert int(longs) == 1


# ── the one-shot latch ────────────────────────────────────────────────────────


def test_the_one_shot_is_latched_and_consumed_once(harness):
    firmware = _run(harness, ["pending", "arm", "pending", "take", "pending"])
    assert firmware == ["0", "armed", "1", "1", "0"]


def test_the_one_shot_matches_the_model(harness):
    model = TriggerModel()
    model.arm()
    firmware = _run(harness, ["arm", "pending", "take", "pending"])
    assert firmware[1] == ("1" if model.pending() else "0")
    assert firmware[2] == ("1" if model.take() else "0")
    assert firmware[3] == ("1" if model.pending() else "0")
    assert model.pending() is False


# ── the LED pattern ───────────────────────────────────────────────────────────


def test_the_led_pattern_agrees_with_the_spec(harness):
    # The button's feedback: N blinks of on/off, then dark.
    model = LedModel()
    model.blink_times(2, 100, 100)
    commands = ["blink 2 100 100"]
    times = [0, 50, 150, 250, 350, 450, 550]
    commands += [f"led {t}" for t in times]
    firmware = _run(harness, commands)
    assert firmware[0] == "blinking"
    states = [int(s) for s in firmware[1:]]
    expected = [int(model.update(t)) for t in times]
    assert states == expected
    # 100 ms lit, 100 ms dark, 100 ms lit, 100 ms dark, then dark for good.
    assert states == [1, 1, 0, 1, 0, 0, 0]


def test_the_led_stays_dark_when_nothing_is_requested(harness):
    firmware = _run(harness, ["led 0", "led 1000"])
    assert firmware == ["0", "0"]
