"""accountsettings/services.py - the rules: what the recovery address's
confirmation mail says, and what a player is shown of their own address.
Pure: no database, no request."""


def confirmation_mail(code, ttl_seconds):
    """(subject, body) of the mail that confirms a new recovery address: the
    code, how long it lives, and that ignoring it changes nothing."""
    return ("Elusion RPG - confirm your recovery address",
            "Your confirmation code is %s\n\n"
            "It expires in %d minutes. If you did not ask for this, you can "
            "ignore it - nothing has changed on your account."
            % (code, ttl_seconds // 60))


def address_view(address, verified, masked):
    """What GET /api/account/email answers: the address masked (`masked`, as
    the caller masked it), whether there is one, and whether it is confirmed."""
    return {
        "email": masked,
        "has_email": address != "",
        "verified": bool(verified),
    }
