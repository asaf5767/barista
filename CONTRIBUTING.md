# Contributing to Barista

Thanks for your interest in contributing! Here's how you can help.

## Ways to Contribute

### Report Compatibility

The most valuable contribution is testing Barista on different De'Longhi models. If you have a machine that works with the "De'Longhi Coffee Link" app:

1. Run `barista scan` and note the device name
2. Run `barista start --address YOUR_ADDRESS` and try brewing
3. [Open an issue](https://github.com/assafakiva/barista/issues/new) with:
   - Your machine model (e.g., "Primadonna Elite ECAM 650.75.MS")
   - Whether scanning found it
   - Whether brewing works
   - Any commands that don't work

### Protocol Discoveries

If you've sniffed new BLE commands or figured out undocumented parameters:

1. Document the raw bytes and what they do
2. Add them to `docs/PROTOCOL.md` via a pull request
3. Bonus: add the implementation to `barista/protocol.py`

### Bug Fixes and Features

1. Fork the repository
2. Create a feature branch: `git checkout -b my-feature`
3. Make your changes
4. Test locally: `pip install -e .` then `barista scan`
5. Submit a pull request

## Development Setup

```bash
git clone https://github.com/assafakiva/barista.git
cd barista
pip install -e ".[dev]"
```

## Code Style

- Python 3.11+ with type hints
- Keep it simple — this is a tool people run at home
- Comments for protocol-level decisions (why certain bytes are what they are)

## Project Structure

```
barista/
  __init__.py       Package metadata
  __main__.py       python -m barista support
  cli.py            CLI entry point
  protocol.py       ECAM BLE protocol implementation
  ble.py            Bluetooth Low Energy driver
  server.py         HTTP API + web UI server
  ui/
    index.html      Web interface
```

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
