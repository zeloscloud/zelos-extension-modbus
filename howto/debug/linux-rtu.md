# Linux: RTU checks

For an RTU connection that opens but times out or reads wrong values. If the port does not open
(the log shows `serial diagnosis:`), start with [DEBUG.md](../../DEBUG.md).

Everything here uses tools present on a stock Linux host. Examples use `/dev/ttyUSB0`, 9600 8N1
and unit 1. Substitute your own.

## 1. Stop the extension first

RS-485 has one master. Linux lets a second program open a port that is in use, and then both
read the same bytes and each loses replies. Stop the extension (Zelos App, or
`zelos extensions stop zeloscloud.zelos-extension-modbus`), then confirm the port is free:

```bash
sudo find /proc/[0-9]*/fd -lname "$(readlink -f /dev/ttyUSB0)"   # no output = free; else /proc/<pid>/fd/<n>
```

## 2. Is traffic moving? (extension running)

On-board UARTs (`ttyAMA*`, `ttyS*`) count bytes. This check only reads counters and works while the extension runs:

```bash
sudo cat /proc/tty/driver/ttyAMA    # ttyS*: /proc/tty/driver/serial; the line number is the port number
```

| Two reads a few seconds apart | Meaning |
|---|---|
| `tx` and `rx` both rising | Requests go out and something answers |
| `tx` rising, `rx` flat | Nothing answers: settings, unit ID or wiring |
| `fe:`, `pe:` or `oe:` appear | Framing, parity or overrun errors: baud or parity mismatch, or noise |

USB adapters (`ttyUSB*`, `ttyACM*`) do not report these counters.

## 3. Read one register

Set the port to the extension's settings. `raw` is required: without it Linux sends a `0a` byte
as `0d 0a` and the device drops the frame.

| Extension | `stty` |
|---|---|
| Baudrate 9600 | `9600` |
| Parity N / E / O | `-parenb` / `parenb -parodd` / `parenb parodd` |
| Stop bits 1 / 2 | `-cstopb` / `cstopb` |
| Data bits 8 / 7 | `cs8` / `cs7` |

```bash
PORT=/dev/ttyUSB0
stty -F "$PORT" 9600 cs8 -cstopb -parenb raw -echo
frame() {  # unit, function code, wire address, count -> the request with its CRC, as printf escapes
  python3 -c 'import struct,sys;u,fc,a,n=map(int,sys.argv[1:]);f=struct.pack(">BBHH",u,fc,a,n);c=0xFFFF
for x in f:
 c^=x
 for _ in range(8):c=(c>>1)^0xA001*(c&1)
print("".join("\\%03o"%b for b in f+struct.pack("<H",c)))' "$@"
}
send() { F=$(frame "$@"); timeout 1 cat "$PORT" | od -An -tx1 & sleep 0.2; printf "$F" > "$PORT"; wait; }
send 1 3 0 1    # unit 1, holding register 1
```

Function codes: `3` holding, `4` input, `1` coils, `2` discrete inputs. The wire address is the
register address - 1 (holding register 40001 is wire 40000); a map with `address_base: 0` uses
its addresses as is. Without `python3`, unit 1 holding register 1 is
`printf '\001\003\000\000\000\001\204\012'`.

| Reply | Meaning |
|---|---|
| `01 03 02 HH LL xx xx` | Register value `0xHHLL`: link, settings and unit ID are right |
| `01 83 02 xx xx` | Exception 02, illegal address: link and settings are right, try an address from the datasheet |
| `01 82 03 xx xx` on a multi-register read | Exception 03: the device limits read length; set `"device": {"max_block_size": N}` in the map |
| Starts with your own request | The adapter echoes what it sends (see below) |
| Nothing | Wrong unit ID, baud or parity, wiring, or the device is off |

A wrong unit ID, baud or parity gets no reply at all; a wrong address gets an exception. To find
unknown settings or unit IDs, use Auto-configure in the extension's config: it tries common
settings and units 1-10 and 247.

## 4. RS-485 failure modes

| Symptom | Likely cause | Check |
|---|---|---|
| No reply, settings verified | A and B swapped; vendors label them A/B, D+/D- or the reverse | Swap the two data wires (harmless) |
| No reply or garbled replies from one unit ID | Two devices share it (many ship as unit 1) | Leave one device on the bus, read it, change its ID, repeat |
| Timeouts while another program polls fine | A second master (PLC, HMI, another tool) on the same bus | Stop the other master; RS-485 allows one |
| Log warns `Short block response ... got 0`; replies start with your request | The adapter echoes what it sends; the extension does not filter echo | Turn echo off on the adapter (jumper or switch), or use one without echo |
| Works on the bench, fails on a long cable or at high baud | Missing 120 ohm termination at both ends, or no bias | Terminate both ends only; lower the baud to confirm |
| Errors on isolated or separately powered devices | No ground reference | Run a GND wire alongside A and B |
| A unit answers only on retry, polls slower than the rate | Device needs a longer gap after other traffic | Raise Advanced `request_delay_ms` |

## 5. Cabling check with two adapters

RTU is binary, so text only proves the cable. Wire two adapters A-A, B-B, GND-GND, set both
ports as in section 3, then:

```bash
cat /dev/ttyUSB1                # terminal 1: shows what arrives
printf 'hello\n' > /dev/ttyUSB0 # terminal 2: hello in terminal 1 = cable and polarity good
```
