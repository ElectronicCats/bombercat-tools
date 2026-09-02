# `bombercat proto`

> Nanopb protobuf sources for the NFCGate relay.

## Quick Start

```sh
bombercat proto gen
```

Regenerates `firmware/core/src/proto/*.pb.{c,h}` from the vendored `.proto` files. Bootstraps a pinned venv on first run.

---

## Subcommands

### `proto gen`

> Regenerate firmware/core/src/proto/*.pb.{c,h} from the vendored .proto files.

```sh
bombercat proto gen
```

```
ℹ Running gen_proto.sh (bootstraps a pinned venv on first run) …
✓ Protobuf sources regenerated.
```

---

### Notes

- Wraps `tools/gen_proto.sh`.
- Requires `bash` on `PATH`.
- First run creates a virtual environment and installs nanopb generator.
