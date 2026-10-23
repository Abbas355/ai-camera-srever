"""Always-on Optiview backend (API + record worker).

Used by Windows scheduled tasks (logon / watchdog) so the server keeps
running in the background and restarts after reboot.

Flutter is the client UI — this process is API-only.
"""

from v380.api.app import main

if __name__ == "__main__":
    main()
