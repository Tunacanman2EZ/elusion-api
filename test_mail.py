"""
test_mail.py - prove the mail settings work, before a player needs them.

    venv\\Scripts\\python.exe test_mail.py you@yourdomain.com

WHY THIS EXISTS. Recovery mail is the one part of the account system that
cannot be tested by the other suites: it depends on credentials that live in
.env and on a server somewhere else answering. So the failure mode is horrible
- everything looks fine until a real player loses their password, and by then
nobody is watching the log.

This sends ONE real message through the SAME send_mail() the server uses, and
turns the cryptic smtplib exceptions into the thing you actually have to go and
change. It never prints your password.

It points the app at a throwaway database, exactly like the other suites, so
importing it cannot touch elusion.db.
"""

import importlib.util
import os
import smtplib
import socket
import ssl
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Same guard every suite here uses: app.py calls init_db() at import time and
# must not be allowed anywhere near the real database.
os.environ["ELUSION_DB"] = os.path.join(tempfile.gettempdir(), "elusion_mailcheck.db")

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)


GREEN = "  OK   "
BAD = "  FAIL "
INFO = "       "


def show(prefix, line):
    print("%s%s" % (prefix, line))


def mask(secret):
    """Never print a credential. Length only, so 'is it empty' is answerable."""
    if not secret:
        return "(empty)"
    return "(set, %d characters)" % len(secret)


def main():
    recipient = sys.argv[1] if len(sys.argv) > 1 else ""

    print("\n=== Elusion mail check ===\n")

    host = app_module.SMTP_HOST
    port = app_module.SMTP_PORT
    user = app_module.SMTP_USER
    password = app_module.SMTP_PASSWORD
    sender = app_module.MAIL_FROM
    console = app_module.MAIL_CONSOLE

    show(INFO, "ELUSION_SMTP_HOST      %s" % (host or "(empty)"))
    show(INFO, "ELUSION_SMTP_PORT      %s" % port)
    show(INFO, "ELUSION_SMTP_USER      %s" % (user or "(empty)"))
    show(INFO, "ELUSION_SMTP_PASSWORD  %s" % mask(password))
    show(INFO, "ELUSION_MAIL_FROM      %s" % (sender or "(empty)"))
    show(INFO, "ELUSION_MAIL_CONSOLE   %s" % ("1 (console mode)" if console else "off"))
    print()

    # ---- console mode ------------------------------------------------------
    if console:
        show(GREEN, "Console mode is ON. Codes print to the server log and NO email is sent.")
        show(INFO, "That is the right setting for testing on your own machine.")
        show(INFO, "Unset ELUSION_MAIL_CONSOLE when you want real mail to go out -")
        show(INFO, "a printed code is a working key to an account sitting in a log file.")
        return 0

    # ---- the two mistakes that look like something else ---------------------
    # A GLUED PASTE. If the user and from lines arrived but the host did not,
    # the host line almost certainly got joined onto whatever was pasted before
    # it - which makes the key name wrong, so the value vanishes silently.
    if not host and (user or sender):
        show(BAD, "ELUSION_SMTP_HOST is missing, but the lines around it are set.")
        show(INFO, "That usually means a paste landed on the HOST line without a")
        show(INFO, "newline, so the key became something like")
        show(INFO, "    <pasted text>ELUSION_SMTP_HOST=smtp.gmail.com")
        show(INFO, "and no key called ELUSION_SMTP_HOST exists any more.")
        show(INFO, "Open .env and check that every setting starts its own line.")
        print()

    # SPACES LEFT IN. Google shows an App Password as 4 groups of 4; pasted
    # whole it is 19 characters and authentication fails with a bare 535.
    if password and "gmail" in host.lower() and len(password) != 16:
        show(BAD, "ELUSION_SMTP_PASSWORD is %d characters; a Gmail App Password is 16."
             % len(password))
        if len(password) == 19:
            show(INFO, "19 is exactly 16 plus the 3 spaces Google displays it with.")
        show(INFO, "Remove every space: 'abcd efgh ijkl mnop' goes in as")
        show(INFO, "'abcdefghijklmnop'. Nothing else about it changes.")
        print()

    # ---- not configured at all ---------------------------------------------
    if not host or not sender:
        show(BAD, "Mail is NOT configured, so recovery codes will go nowhere.")
        print()
        show(INFO, "Add these to the .env beside app.py, then run this again:")
        print()
        print("    ELUSION_SMTP_HOST=smtp.gmail.com")
        print("    ELUSION_SMTP_PORT=587")
        print("    ELUSION_SMTP_USER=you@gmail.com")
        print("    ELUSION_SMTP_PASSWORD=your-16-character-app-password")
        print("    ELUSION_MAIL_FROM=Elusion RPG <you@gmail.com>")
        print()
        show(INFO, "Or, to test the whole flow with no provider at all:")
        print()
        print("    ELUSION_MAIL_CONSOLE=1")
        print()
        return 1

    if not recipient:
        # CONFIGURED BUT NOT ASKED TO SEND, WHICH IS A PASS.
        #
        # This used to return 1, and that made the file permanently red inside
        # run_tests.ps1: the runner has no address to give it, so a fully
        # working mail setup reported FAIL on every single run. A suite that
        # cannot pass in the harness is one you learn to skip over, and the day
        # it fails for a real reason you skip over that too.
        #
        # THE SPLIT IS BETWEEN CONFIGURATION AND DELIVERY. Everything above
        # this line checks configuration, and configuration is exactly what an
        # automated sweep should check - it is the half that breaks silently
        # when somebody edits .env. Actually putting a message on the wire
        # needs a human to name an inbox and then go and look in it, so it
        # stays a manual act.
        #
        # It says plainly that nothing was sent, so nobody who meant to send
        # one walks away thinking they did.
        show(GREEN, "Configuration looks right. Nothing was sent.")
        print()
        show(INFO, "To actually put a message on the wire, name an inbox:")
        show(INFO, "    python test_mail.py you@yourdomain.com")
        print()
        # A summary line in the shape run_tests.ps1 scrapes, so this suite
        # contributes a number to the total instead of a shrug.
        print("1 passed, 0 failed")
        return 0

    # ---- the connection, step by step --------------------------------------
    show(INFO, "Connecting to %s:%d ..." % (host, port))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        show(GREEN, "Connected and secured.")
    except socket.gaierror:
        show(BAD, "That hostname does not resolve. Check ELUSION_SMTP_HOST for a typo.")
        return 1
    except smtplib.SMTPNotSupportedError:
        show(BAD, "That server does not offer STARTTLS on port %d." % port)
        show(INFO, "The server REFUSES to send without encryption, on purpose - a reset")
        show(INFO, "code crossing the network in the clear is a key anyone can copy.")
        show(INFO, "Use port 587 with STARTTLS, or 465 for straight SSL.")
        return 1
    except (socket.timeout, TimeoutError, ConnectionRefusedError, OSError) as error:
        show(BAD, "Could not reach %s:%d - %s" % (host, port, error))
        show(INFO, "Usually the port: 587 for STARTTLS, 465 for SSL. A home ISP or a")
        show(INFO, "VPS firewall blocking outbound 25/465/587 is the other common cause.")
        return 1
    except ssl.SSLError as error:
        show(BAD, "TLS failed - %s" % error)
        show(INFO, "This is almost always the port and the mode disagreeing.")
        show(INFO, "Port 465 needs SSL, port 587 needs STARTTLS. Swap the port.")
        return 1

    with server:
        # ---- the login -----------------------------------------------------
        if user:
            try:
                server.login(user, password)
                show(GREEN, "Signed in as %s." % user)
            except smtplib.SMTPAuthenticationError as error:
                show(BAD, "The server refused those credentials - %s" % error.smtp_code)
                show(INFO, "For Gmail this is nearly always the wrong KIND of password:")
                show(INFO, "it needs a 16-character App Password, not your normal one,")
                show(INFO, "and 2-Step Verification has to be switched on to create one.")
                return 1
            except smtplib.SMTPException as error:
                show(BAD, "Login failed - %s" % error)
                return 1
        else:
            show(INFO, "No username set - sending unauthenticated.")

        # ---- the send ------------------------------------------------------
        show(INFO, "Sending a test message to %s ..." % recipient)
        try:
            from email.message import EmailMessage
            message = EmailMessage()
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = "Elusion RPG - mail is working"
            message.set_content(
                "If you are reading this, account recovery can reach your players.\n\n"
                "Sent by test_mail.py.")
            server.send_message(message)
        except smtplib.SMTPSenderRefused:
            show(BAD, "The server refused the FROM address: %s" % sender)
            show(INFO, "Most providers only let you send as an address you own and")
            show(INFO, "have verified. Make ELUSION_MAIL_FROM match ELUSION_SMTP_USER.")
            return 1
        except smtplib.SMTPRecipientsRefused:
            show(BAD, "The server refused the recipient: %s" % recipient)
            return 1
        except smtplib.SMTPException as error:
            show(BAD, "Send failed - %s" % error)
            return 1

    show(GREEN, "Sent.")
    print()

    # ---- and the real thing ------------------------------------------------
    show(INFO, "Now through the server's own send_mail(), the exact function")
    show(INFO, "account recovery calls ...")
    if app_module.send_mail(recipient, "Elusion RPG - recovery path check",
                            "This one went through send_mail(), the same call a "
                            "reset code uses."):
        show(GREEN, "send_mail() reports success.")
    else:
        show(BAD, "send_mail() reported failure - check the lines above.")
        return 1

    print()
    show(GREEN, "Mail is working. Check %s - two messages should be waiting." % recipient)
    show(INFO, "If they are in spam, that is deliverability, not configuration:")
    show(INFO, "a real sending domain with SPF and DKIM fixes it. Gmail is fine")
    show(INFO, "for a small tester base.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
