"""Slash commands and conversation starters.

Commands are answered from constants, never the LLM: a customer's first
impression should be instant and identical every time, and /start costs nothing
if it never leaves the process.

The suggestion lists exist because an empty chat is the hardest moment in a
sales conversation. "Ask me anything" gets no reply; three concrete questions
get a click.
"""

from __future__ import annotations

from app.prompts import SALES_PHONE, WEBSITE, WHATSAPP_NUMBER

# Every catalog area a customer can actually be helped with. Kept in step with
# the knowledge base sections so a suggestion never leads to "I don't have that".
CATEGORIES = [
    "Total stations, auto levels & survey accessories",
    "Bar bending, cutting & stirrup machines",
    "Rollers, plate compactors & rammers",
    "Concrete vibrators, cutters & power trowels",
    "Mini cranes, hoists & lifting equipment",
    "Civil lab & material testing equipment",
    "Construction chemicals & anchor fasteners",
    "Safety items & road safety products",
]

START_MESSAGE = """Welcome to Divine Empire India 👋

We supply construction equipment, survey instruments, civil lab equipment, construction chemicals and safety items — serving 3,283+ customers since 2015 from Raipur, Bhubaneswar and Guwahati.

I can help you with:
• Product details, specifications and approximate pricing
• Choosing the right equipment for your project
• Comparing options within your budget
• Connecting you with our sales team for a formal quote

You can write in English, Hindi or Hinglish — whichever you prefer.

What are you looking for? For example:
"Bar bending machine for 32mm TMT"
"Total station under 3 lakh"
"Setting up a new site QC lab"
"""

HELP_MESSAGE = f"""Here's how I can help 👇

*What you can ask me*
• Prices and specifications for any product we stock
• Which machine suits your project, site or budget
• Comparisons — "IM-55 vs IM-105", "walk-behind vs ride-on roller"
• Bundles — everything needed for an RCC site or a new testing lab
• Service, calibration, rental and NABL accreditation support

*What we supply*
{chr(10).join("• " + c for c in CATEGORIES)}

*Talk to a person*
For formal quotations, negotiated pricing or bulk orders, just ask and I'll connect you with our sales team.
📞 {SALES_PHONE}
💬 WhatsApp {WHATSAPP_NUMBER}
🌐 {WEBSITE}
Monday–Sunday, 9:30 AM – 6:30 PM

*Commands*
/start — start over
/products — what we supply
/contact — sales team details
/clear — clear our conversation and start fresh
/stop — stop receiving messages

You can write in English, Hindi or Hinglish.
"""

PRODUCTS_MESSAGE = f"""What we supply 🏗️

{chr(10).join("• " + c for c in CATEGORIES)}

We stock brands including Sokkia, Wacker Neuson, Fischer, Bosch, Sika and our own ManiQuip and Divine ranges.

Tell me your project type and I'll suggest what fits — for example "RCC building site", "road work", or "new testing lab".
"""

CLEARED_MESSAGE = """Conversation cleared 🧹

I've forgotten what we discussed. Starting fresh — what are you looking for?

Your enquiry details stay with our sales team, so nothing you asked for is lost.
"""

CONTACT_MESSAGE = f"""Divine Empire India Pvt. Ltd. 📍

*Sales*
📞 {SALES_PHONE}
💬 WhatsApp {WHATSAPP_NUMBER}
🌐 {WEBSITE}

*Head office*
401-402, Lal Ganga Midas, Fafadih, Raipur, Chhattisgarh 492009
Branches: Bhubaneswar (Odisha) • Guwahati (Assam)

*Hours*
Monday–Sunday, 9:30 AM – 6:30 PM (Sunday service available)

GST registered. Payment by NEFT/RTGS/IMPS, cheque/DD or cash.
Domestic (India) supply only.
"""

# Commands deliberately routed to the agent instead of answered from a constant.
# /stop must reach record_opt_out so the opt-out is persisted and enforced —
# a canned "you've been unsubscribed" that writes nothing is a compliance lie.
PASS_THROUGH_COMMANDS = {"/stop"}

# Commands with a side effect the agent performs before replying. Handled in
# agent.handle_message rather than here, since this module stays pure.
STATEFUL_COMMANDS = {"/clear"}

COMMAND_RESPONSES = {
    "/start": START_MESSAGE,
    "/help": HELP_MESSAGE,
    "/products": PRODUCTS_MESSAGE,
    "/contact": CONTACT_MESSAGE,
}

# Pasted into BotFather via /setcommands.
BOTFATHER_COMMANDS = """start - Start a conversation
help - What I can help you with
products - What we supply
contact - Sales team contact details
clear - Clear the conversation and start fresh
stop - Stop receiving messages"""


def handle_command(text: str) -> str | None:
    """Return a canned reply for a slash command, or None to let the agent run.

    Telegram sends "/start@botname" in groups, so the mention is stripped.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    command = stripped.split()[0].split("@")[0].lower()
    if command in PASS_THROUGH_COMMANDS or command in STATEFUL_COMMANDS:
        return None
    return COMMAND_RESPONSES.get(command)


def parse_command(text: str) -> str | None:
    """Return the normalised command name, or None if this is not a command."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    return stripped.split()[0].split("@")[0].lower()
