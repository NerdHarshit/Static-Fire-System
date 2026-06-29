"""
DJS Impulse — Static Fire Ground Station
Connects to Pico W over TCP ethernet.
- Sends commands via keyboard
- Displays all incoming messages in terminal
- Auto-saves DATA: lines to Ethernet_data.csv as they arrive
- Plots thrust curve after LOG_STOP is received

Usage:
    python ground_station.py

Requirements:
    pip install matplotlib
"""

import socket
import threading
import csv
import os
import sys
import time
import datetime
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# =============================================================================
#  CONFIG
# =============================================================================
PICO_IP   = "192.168.1.60"
PICO_PORT = 8080
CSV_FILE  = "Ethernet_data.csv"
TIMEOUT   = 10   # seconds for initial connection

# =============================================================================
#  GLOBALS
# =============================================================================
data_t    = []   # timestamps (ms)
data_raw  = []   # raw thrust (N)
data_filt = []   # filtered thrust (N)

session_complete = threading.Event()
stop_rx          = threading.Event()
sock             = None
csv_writer       = None
csv_file_handle  = None

# ANSI colours for terminal output
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# =============================================================================
#  HELPERS
# =============================================================================

def ts():
    """Current time string for terminal prefix."""
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def print_rx(line):
    """Print a received line with colour coding."""
    line = line.strip()
    if not line:
        return

    if line.startswith("DATA:"):
        # Print data lines in cyan but compact — don't flood terminal
        parts = line[5:].split(",")
        if len(parts) == 3:
            print(f"  {CYAN}[{ts()}] {line}{RESET}")
        else:
            print(f"  {CYAN}[{ts()}] {line}{RESET}")

    elif line.startswith("ERR:"):
        print(f"\n  {RED}{BOLD}[{ts()}] {line}{RESET}\n")

    elif line.startswith("OK:"):
        print(f"  {GREEN}[{ts()}] {line}{RESET}")

    elif line.startswith("STATE:"):
        print(f"\n  {YELLOW}{BOLD}[{ts()}] *** {line} ***{RESET}\n")

    elif line.startswith("T-"):
        print(f"  {YELLOW}[{ts()}] COUNTDOWN {line}{RESET}")

    elif line == "COMPLETE":
        print(f"\n  {GREEN}{BOLD}[{ts()}] *** LOGGING COMPLETE ***{RESET}\n")

    else:
        print(f"  [{ts()}] {line}")

def send_command(cmd):
    """Send a newline-terminated command to the Pico."""
    try:
        sock.sendall((cmd.strip() + "\n").encode())
        print(f"\n  {BOLD}>>> SENT: {cmd.strip()}{RESET}")
    except Exception as e:
        print(f"\n  {RED}Send error: {e}{RESET}")

# =============================================================================
#  RECEIVER THREAD
#  Runs in background, parses every line from the Pico.
#  DATA: lines → parsed and appended to lists + written to CSV immediately.
#  COMPLETE line → sets session_complete event to trigger plot.
# =============================================================================

def receiver():
    global csv_writer

    buffer = ""
    while not stop_rx.is_set():
        try:
            chunk = sock.recv(4096).decode(errors="replace")
            if not chunk:
                print(f"\n  {RED}Connection closed by Pico.{RESET}")
                stop_rx.set()
                break
            buffer += chunk
        except socket.timeout:
            continue
        except Exception as e:
            if not stop_rx.is_set():
                print(f"\n  {RED}Receive error: {e}{RESET}")
            break

        # Process all complete lines in buffer
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue

            print_rx(line)

            # Parse DATA lines and write to CSV immediately
            if line.startswith("DATA:"):
                parts = line[5:].split(",")
                if len(parts) == 3:
                    try:
                        t_ms   = int(parts[0].strip())
                        raw_n  = float(parts[1].strip())
                        filt_n = float(parts[2].strip())
                        data_t.append(t_ms)
                        data_raw.append(raw_n)
                        data_filt.append(filt_n)
                        if csv_writer:
                            csv_writer.writerow([t_ms, raw_n, filt_n])
                            csv_file_handle.flush()  # write immediately, don't buffer
                    except ValueError:
                        pass  # malformed line, skip

            # Session complete
            if line == "COMPLETE":
                session_complete.set()

# =============================================================================
#  POST-FIRE PLOT
# =============================================================================

def plot_results():
    if not data_t:
        print(f"\n  {YELLOW}No data to plot.{RESET}")
        return

    t_sec = [t / 1000.0 for t in data_t]

    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("#1e1e2e")
    ax.set_facecolor("#1e1e2e")

    ax.plot(t_sec, data_raw,  color="#6c7086", linewidth=0.8,
            label="Raw Thrust", alpha=0.7)
    ax.plot(t_sec, data_filt, color="#89b4fa", linewidth=1.8,
            label="Filtered Thrust (EMA)")

    # Mark peak
    if data_filt:
        peak_idx = data_filt.index(max(data_filt))
        ax.annotate(
            f"Peak: {data_filt[peak_idx]:.2f} N\n@ {t_sec[peak_idx]:.3f} s",
            xy=(t_sec[peak_idx], data_filt[peak_idx]),
            xytext=(t_sec[peak_idx] + 0.1, data_filt[peak_idx] * 0.95),
            color="#a6e3a1",
            fontsize=9,
            arrowprops=dict(arrowstyle="->", color="#a6e3a1", lw=1.2)
        )

    ax.set_xlabel("Time (s)", color="#cdd6f4", fontsize=11)
    ax.set_ylabel("Thrust (N)", color="#cdd6f4", fontsize=11)
    ax.set_title("DJS Impulse — Static Fire Thrust Curve", color="#cdd6f4",
                 fontsize=13, fontweight="bold")
    ax.legend(facecolor="#313244", edgecolor="#45475a",
              labelcolor="#cdd6f4", fontsize=10)
    ax.tick_params(colors="#cdd6f4")
    ax.xaxis.set_minor_locator(ticker.AutoMinorLocator())
    ax.yaxis.set_minor_locator(ticker.AutoMinorLocator())
    ax.grid(True, which="major", color="#45475a", linewidth=0.6)
    ax.grid(True, which="minor", color="#313244", linewidth=0.3)
    for spine in ax.spines.values():
        spine.set_edgecolor("#45475a")

    # Stats box
    total_samples = len(data_t)
    duration_s    = (data_t[-1] - data_t[0]) / 1000.0 if len(data_t) > 1 else 0
    avg_rate      = total_samples / duration_s if duration_s > 0 else 0
    stats_text    = (
        f"Samples : {total_samples}\n"
        f"Duration: {duration_s:.3f} s\n"
        f"Avg rate: {avg_rate:.1f} Hz\n"
        f"Peak raw: {max(data_raw):.2f} N\n"
        f"Peak filt:{max(data_filt):.2f} N"
    )
    ax.text(0.99, 0.97, stats_text, transform=ax.transAxes,
            fontsize=8, verticalalignment="top", horizontalalignment="right",
            color="#cdd6f4",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#313244",
                      edgecolor="#45475a", alpha=0.9))

    plt.tight_layout()

    plot_file = CSV_FILE.replace(".csv", "_plot.png")
    plt.savefig(plot_file, dpi=150, facecolor=fig.get_facecolor())
    print(f"\n  {GREEN}Plot saved to: {plot_file}{RESET}")
    plt.show()

# =============================================================================
#  COMMAND MENU
# =============================================================================

MENU = f"""
{BOLD}╔══════════════════════════════════════╗
║   DJS Impulse Ground Station         ║
║   Commands                           ║
╠══════════════════════════════════════╣
║  i  → INIT                           ║
║  t  → TARE                           ║
║  a  → ARM                            ║
║  l  → LOG_START                      ║
║  f  → IGNITE (FIRE)                  ║
║  s  → LOG_STOP                       ║
║  ?  → STATUS                         ║
║  d  → DISARM                         ║
║  p  → Plot current data              ║
║  q  → Quit                           ║
╚══════════════════════════════════════╝{RESET}
"""

KEY_MAP = {
    "i": "INIT",
    "t": "TARE",
    "a": "ARM",
    "l": "LOG_START",
    "f": "IGNITE",
    "s": "LOG_STOP",
    "?": "STATUS",
    "d": "DISARM",
}

# =============================================================================
#  MAIN
# =============================================================================

def main():
    global sock, csv_writer, csv_file_handle

    print(f"\n{BOLD}DJS Impulse — Static Fire Ground Station{RESET}")
    print(f"Connecting to {PICO_IP}:{PICO_PORT} ...")

    # Connect
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((PICO_IP, PICO_PORT))
        sock.settimeout(1.0)   # non-blocking timeout for recv loop
        print(f"{GREEN}Connected.{RESET}\n")
    except Exception as e:
        print(f"{RED}Connection failed: {e}{RESET}")
        print("Check: Pico is powered, ethernet cable plugged in,")
        print(f"       laptop IP is 192.168.1.x, Pico IP is {PICO_IP}")
        sys.exit(1)

    # Open CSV
    csv_exists = os.path.exists(CSV_FILE)
    csv_file_handle = open(CSV_FILE, "a", newline="")
    csv_writer = csv.writer(csv_file_handle)
    if not csv_exists or os.path.getsize(CSV_FILE) == 0:
        csv_writer.writerow(["t_ms", "thrust_raw_N", "thrust_filt_N"])
        csv_file_handle.flush()
    print(f"Logging data to: {CYAN}{CSV_FILE}{RESET}\n")

    # Start receiver thread
    rx_thread = threading.Thread(target=receiver, daemon=True)
    rx_thread.start()

    # Show menu
    print(MENU)

    # Command loop
    try:
        while not stop_rx.is_set():
            try:
                key = input("  cmd> ").strip().lower()
            except EOFError:
                break

            if key == "q":
                print("  Quitting...")
                break

            elif key == "p":
                plot_results()

            elif key in KEY_MAP:
                cmd = KEY_MAP[key]

                # Safety confirmation for IGNITE
                if cmd == "IGNITE":
                    confirm = input(
                        f"\n  {RED}{BOLD}CONFIRM IGNITE — type YES to proceed: {RESET}"
                    ).strip()
                    if confirm != "YES":
                        print("  Ignition aborted.\n")
                        continue

                send_command(cmd)

                # After LOG_STOP, wait briefly then auto-plot
                if cmd == "LOG_STOP":
                    print(f"\n  Waiting for COMPLETE confirmation from Pico...")
                    session_complete.wait(timeout=10)
                    print(f"\n  {GREEN}Session ended. Generating plot...{RESET}")
                    time.sleep(0.5)
                    plot_results()

            else:
                print(f"  {YELLOW}Unknown key. Press Enter to see menu.{RESET}")
                print(MENU)

    except KeyboardInterrupt:
        print("\n  Interrupted.")

    finally:
        stop_rx.set()
        if csv_file_handle:
            csv_file_handle.close()
            print(f"\n  CSV closed: {CSV_FILE}")
        try:
            sock.close()
        except Exception:
            pass
        print("  Disconnected. Goodbye.\n")

if __name__ == "__main__":
    main()
