def ticket_channel(ticket) -> str | None:
    """The channel a ticket arrived on -- Zendesk's ``via.channel``: "web", "email", "api",
    "facebook", ... -- or None when the payload does not say.

    Without it a caller cannot tell a Facebook Messenger ticket from a web-form one, and ends
    up guessing search syntax for the channel instead of reading it off the ticket.

    Accepts a zenpy Ticket (whose ``via`` is a Via object, or a dict when built by hand) or a
    raw API ticket dict. Anything that is not a string -- a test double, a missing field --
    is None, never a value that would not serialise.
    """
    via = ticket.get("via") if isinstance(ticket, dict) else getattr(ticket, "via", None)
    channel = via.get("channel") if isinstance(via, dict) else getattr(via, "channel", None)
    return channel if isinstance(channel, str) else None
