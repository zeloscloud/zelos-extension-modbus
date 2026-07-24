# Serial / USB Debugging

The extension logs a short `serial diagnosis:` block when an RTU connect fails. This page maps
those findings (and their errnos) to causes and checks. None of the one-liners are conclusive on
their own — use the checks below to confirm.

## First, three commands

```bash
ls -l /dev/serial/by-id/ /dev/ttyUSB* /dev/ttyACM*   # which adapters exist; stable name → node mapping
sudo dmesg | tail -50                                 # USB disconnect / re-enumeration / driver errors
lsof /dev/ttyUSB0                                     # who holds the port (run as root for all users)
```

## Failure modes by symptom

| Symptom | Likely causes | Checks |
|---|---|---|
| Node missing (`ENOENT`) | Adapter unplugged, or re-enumerated to a new number (`ttyUSB0` → `ttyUSB1`) | `ls /dev/ttyUSB*`; `dmesg` shows `USB disconnect` then a new attach |
| Open fails `EIO` | Usually a stale node (device dropped off the bus while the port was open); can also be a wedged adapter, driver fault, or failing hardware | `ls /sys/class/tty/ttyUSB0/device` — missing = stale node; replug and re-check `dmesg` |
| Open fails `EACCES` | User not in the port's group (`dialout`/`uucp`), or a udev rule tightened the mode | `ls -l <port>`; `id`; fix: `sudo usermod -aG dialout $USER` + re-login |
| Open fails `EBUSY` | Another process holds the port: a previous extension instance, ModemManager probing, a terminal session (`screen`) left attached | `lsof <port>`; `systemctl status ModemManager`; detached `screen -ls` |
| Opens cleanly, no data | Wrong baud/parity/unit-id, RS-485 A/B swapped, TX/RX swapped, missing termination, device not powered | Single-register probe: read holding register 0 with `mbpoll -m rtu -b <baud> -P none -a <unit> -0 -r 0 -c 1 -1 <port>` (match your configured parity/stopbits — mbpoll defaults to even) |
| Adapter keeps disconnecting | USB autosuspend, unpowered hub, marginal cable, counterfeit FTDI/CH340 resetting under load | `journalctl -k \| grep -iE 'usb\|tty'` timestamps vs data gaps; try a powered hub / different cable and port |
| Node vanishes right after plug-in | `brltty` claims CH341/CP210x adapters on newer Ubuntu | `dmesg` shows attach then immediate disconnect by brltty; remove/mask brltty |

## Prevent re-enumeration breakage

Configure the port by its stable identity instead of a `ttyUSBn` number, and the extension's
retry loop will reconnect on its own after any glitch:

```json
"serial_port": "/dev/serial/by-id/usb-FTDI_USB_Serial_XXXX-if00-port0"
```

## Notes

- `stty` configures a port and exits — it is not an interactive session (`screen <port> <baud>` is),
  and neither proves Modbus comms; only a register read does (see the `mbpoll` probe above).
- `dmesg` may require root (`kernel.dmesg_restrict=1`); `journalctl -k` is the fallback.
- The extension's holder scan sees same-user processes without root; absence of a reported holder
  is not proof the port is free.
