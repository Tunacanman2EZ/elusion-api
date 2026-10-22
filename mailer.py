"""
mailer.py - the one way this server's mail leaves the building: the settings
it reads and the SMTP conversation it has (0.74.0).

    settings = mailer.settings_from(os.environ)
    if mailer.can_send(settings):
        sent, error = mailer.deliver(settings, address, subject, body)

WHY THIS IS ITS OWN FILE
------------------------
It was the middle of app.py's send_mail(), and for the server alone that was
fine. Then the canary went on a timer (canary.py --mail): when the gold totals
stop adding up it mails the owner, and it must do that the way the server does
- the same settings, read by the same rules, and the same SMTP conversation -
or the first time the server's mail is fixed the canary's stays broken, and the
alarm that matters most is the one that was never tested. The canary cannot
import app.py to get it: importing app.py runs init_db(), which WRITES the
database, and the canary's whole discipline is that it can never be the thing
that changed what it is watching. So the conversation lives here, and both
call it.

NOTHING HERE PRINTS OR LOGS. A credential and an address are both things a log
must not hold; deliver() hands back what went wrong and the caller decides what
to say - app.py logs it the way it always did, and the canary says only how
many of the owner's addresses it reached.

THE SETTINGS (names only - the values live in the server's settings file and
nowhere else):

    ELUSION_SMTP_HOST      smtp.gmail.com, smtp.sendgrid.net, ...
    ELUSION_SMTP_PORT      587 for STARTTLS (the default), 465 for SSL
    ELUSION_SMTP_USER      the login for that server
    ELUSION_SMTP_PASSWORD  its password or API key
    ELUSION_MAIL_FROM      the address players will see it come from
    ELUSION_MAIL_CONSOLE   1: print instead of sending (a PC, never the server)
"""

import smtplib
from email.message import EmailMessage

# How long one SMTP conversation may take, the same fifteen seconds the
# server's send_mail() always waited.
TIMEOUT = 15


def settings_from(environ):
    """The mail settings in `environ` (any mapping), read exactly as app.py
    reads them at boot: stripped, the port 587 unless it says otherwise, the
    password as given."""
    def text(name):
        return str(environ.get(name, "") or "").strip()

    try:
        port = int(text("ELUSION_SMTP_PORT") or 587)
    except ValueError:
        port = 587
    return {
        "host": text("ELUSION_SMTP_HOST"),
        "port": port,
        "user": text("ELUSION_SMTP_USER"),
        "password": str(environ.get("ELUSION_SMTP_PASSWORD", "") or ""),
        "sender": text("ELUSION_MAIL_FROM"),
        "console": text("ELUSION_MAIL_CONSOLE") == "1",
    }


def can_send(settings):
    """True when there is somewhere to send: a provider, or the console."""
    return bool(settings.get("console")) or bool(settings.get("host") and settings.get("sender"))


def message_for(settings, to_address, subject, body):
    """The message as it goes out."""
    message = EmailMessage()
    message["From"] = settings["sender"]
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)
    return message


def deliver(settings, to_address, subject, body, smtp=None):
    """
    One message over SMTP. (True, None) when it left the building, else
    (False, the exception) - never raises, never prints.

    SSL on 465, STARTTLS on anything else; a login only when a user is set.
    Exactly the conversation app.py's send_mail() has always had. `smtp` is
    the smtplib module, a seam for the suites.

    CONSOLE MODE IS NOT HANDLED HERE: what to print instead of sending is the
    caller's to decide, because only the caller knows what it may print.
    """
    smtp = smtp or smtplib
    try:
        message = message_for(settings, to_address, subject, body)
        if int(settings["port"]) == 465:
            server = smtp.SMTP_SSL(settings["host"], int(settings["port"]), timeout=TIMEOUT)
        else:
            server = smtp.SMTP(settings["host"], int(settings["port"]), timeout=TIMEOUT)
            server.starttls()
        with server:
            if settings.get("user"):
                server.login(settings["user"], settings["password"])
            server.send_message(message)
        return True, None
    except Exception as error:  # noqa: BLE001
        # Deliberately broad: smtplib raises a dozen different things and every
        # one of them means the same thing here.
        return False, error
