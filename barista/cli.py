"""
Barista CLI - Control your De'Longhi coffee machine.

Usage:
    barista scan                         Find your machine
    barista start --address XX:XX        Start the server + web UI
    barista start --address XX:XX -p 9090  Custom port
"""

import asyncio
import sys

from barista.server import cmd_scan, cmd_serve


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print("barista - Control your De'Longhi coffee machine")
        print()
        print("Usage:")
        print("  barista scan                           Find BLE coffee machines")
        print("  barista start --address XX:XX:XX:XX    Start server + web UI")
        print("  barista start --address XX:XX -p 9090  Custom port")
        print()
        print("Web UI:  http://localhost:8080 (default)")
        print()
        sys.exit(0)

    command = sys.argv[1]

    if command == "scan":
        asyncio.run(cmd_scan())

    elif command in ("start", "serve"):
        address = None
        port = 8080
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] in ("--address", "-a") and i + 1 < len(sys.argv):
                address = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] in ("--port", "-p") and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1

        if not address:
            print("Error: --address is required.")
            print("  Run 'barista scan' first to find your machine.")
            sys.exit(1)

        asyncio.run(cmd_serve(address, port))

    else:
        print(f"Unknown command: {command}")
        print("  Run 'barista --help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    main()
