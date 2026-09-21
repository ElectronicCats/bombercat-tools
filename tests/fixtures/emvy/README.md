# Fixtures EMVyBomberCat — transcripciones seriales de campo (Fase 0)

Estas transcripciones son la **verdad de campo** capturada de una BomberCat real con
EMVyBomberCat flasheado. Sirven como fixtures para los tests sin hardware (`FakeSerial`,
ver `tests/conftest.py`) y como **línea base pre-migración** para verificar la retrocompat
del puente `EmvyLink` cuando el firmware pase al framing canónico (Opción B, §4/§7 del plan).

## Cómo capturar

1. Conecta la placa y abre un monitor serie a **115200 8N1** (`screen /dev/ttyACM0 115200`,
   `minicom -D /dev/ttyACM0 -b 115200`, o `bombercat` con `-v`).
2. Envía cada verbo del §2.3 del plan **literalmente** y pega la respuesta cruda (incluidas
   las líneas `# …` de log) en el `.txt` correspondiente. Una transcripción por archivo.
3. Captura cada verbo **con** y **sin** tarjeta cuando aplique (sufijo `_card` / `_nocard`).

## Convención de nombres

| Archivo | Verbo enviado | Escenario |
|---|---|---|
| `ping.txt`            | `ping`                         | handshake |
| `info.txt`           | `info`                         | discovery |
| `identify.txt`       | `identify`                     | discovery |
| `wait_card.txt`      | `WAIT 30000`                   | con tarjeta ISO-DEP |
| `wait_nocard.txt`    | `WAIT 3000`                    | sin tarjeta (→ `ERR:NOCARD`) |
| `apdu_card.txt`      | `WAIT` → `APDU:00A4040007A0000000031010` → `RELEASE` | select PPSE/AID con tarjeta |
| `apdu_nocard.txt`    | `APDU:00A4040007A0000000031010` | sin `WAIT` previo (→ `ERR:NOCARD`) |
| `apdu_badapdu.txt`   | `APDU:00`                      | APDU inválido (→ `ERR:BADAPDU`) |
| `release.txt`        | `RELEASE`                      | → `OK` |
| `tag_card.txt`       | `TAG`                          | con tag |
| `tag_nocard.txt`     | `TAG`                          | sin tag |
| `mag.txt`            | `MAG:<t1>\|<t2>`               | → `OK` |
| `scan_card.txt`      | `SCAN 500`                     | flujo EMV completo → `JSON_START/END` |
| `scan_timeout.txt`   | `SCAN 500`                     | sin tarjeta (→ `# ERROR: Timeout`) |
| `cardscan_card.txt`  | `CARDSCAN`                     | → `EMU:SCANNED …` |
| `cardscan_nocard.txt`| `CARDSCAN`                     | → `ERR:NOCARD` |
| `emu_ndef.txt`       | `EMU:D101…` → `STOP`           | stream `EMU:RX/TX/MSG-SENT` → `EMU:DONE` |
| `emuemv.txt`         | `EMUEMV` → `STOP`              | tarjeta EMV de prueba, stream → `EMU:DONE` |
| `stop.txt`           | `STOP`                         | → `OK` |
| `nfcinfo.txt`        | `NFCINFO`                      | diagnóstico PN7150 |
| `reboot.txt`         | `REBOOT`                       | → `# REBOOT` (el USB se re-enumera) |

Guarda también la salida real de `bombercat status`, `bombercat device list` y
`bombercat identify` (tarea 1 de Fase 0) en `status_baseline.txt`.
