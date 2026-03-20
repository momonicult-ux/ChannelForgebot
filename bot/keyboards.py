"""
bot/keyboards.py — Inline keyboards.
v6: Expanded rewrite (6 buttons), onboarding keyboards, viral CTA button.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ─── REWRITE WORKFLOW (v6: expanded to 6 buttons) ───────────────────────────

def rewrite_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Rewrite", callback_data="rewrite:another"),
            InlineKeyboardButton("🔥 More Viral", callback_data="rewrite:viral"),
        ],
        [
            InlineKeyboardButton("✂️ Shorter", callback_data="rewrite:shorter"),
            InlineKeyboardButton("📣 Add CTA", callback_data="rewrite:cta"),
        ],
        [
            InlineKeyboardButton("💗 More Emotional", callback_data="rewrite:emotional"),
            InlineKeyboardButton("🧠 Smarter Tone", callback_data="rewrite:smarter"),
        ],
    ])


# ─── REGENERATE ──────────────────────────────────────────────────────────────

def regenerate_keyboard(command: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Generate new batch", callback_data=f"regen:{command}"),
    ]])


# ─── CHANNEL MODE ────────────────────────────────────────────────────────────

def channel_publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Publish", callback_data="channel:publish"),
        InlineKeyboardButton("🔄 Rewrite", callback_data="channel:rewrite"),
        InlineKeyboardButton("❌ Cancel", callback_data="channel:cancel"),
    ]])


# ─── VIRAL CTA BUTTON (v6) ──────────────────────────────────────────────────

def viral_cta_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """
    Shown below every generated post.
    'Create Your Own Post' opens the bot in a new chat.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✨ Create Your Own Post",
            url=f"https://t.me/{bot_username}",
        ),
    ]])


# ─── ONBOARDING KEYBOARDS (v6 — /start flow) ────────────────────────────────

def onboarding_niche_keyboard() -> InlineKeyboardMarkup:
    """Step 1 of /start onboarding: channel niche."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Crypto", callback_data="onboard:niche:Crypto"),
            InlineKeyboardButton("AI", callback_data="onboard:niche:AI / Tech"),
        ],
        [
            InlineKeyboardButton("Business", callback_data="onboard:niche:Business"),
            InlineKeyboardButton("Motivation", callback_data="onboard:niche:Motivation"),
        ],
        [
            InlineKeyboardButton("News", callback_data="onboard:niche:News"),
            InlineKeyboardButton("Memes", callback_data="onboard:niche:Memes & humor"),
        ],
    ])


def onboarding_audience_keyboard() -> InlineKeyboardMarkup:
    """Step 2: audience level."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Beginner", callback_data="onboard:audience:Beginners")],
        [InlineKeyboardButton("Intermediate", callback_data="onboard:audience:Intermediate")],
        [InlineKeyboardButton("Advanced", callback_data="onboard:audience:Experts")],
    ])


def onboarding_tone_keyboard() -> InlineKeyboardMarkup:
    """Step 3: tone."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Professional", callback_data="onboard:tone:Professional"),
            InlineKeyboardButton("Casual", callback_data="onboard:tone:Casual & fun"),
        ],
        [
            InlineKeyboardButton("Funny", callback_data="onboard:tone:Funny"),
            InlineKeyboardButton("Bold", callback_data="onboard:tone:Bold & edgy"),
        ],
    ])


# ─── PROFILE SETUP (existing /profile flow) ─────────────────────────────────

def profile_niche_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Crypto", callback_data="profile:niche:Crypto"),
            InlineKeyboardButton("AI / Tech", callback_data="profile:niche:AI / Tech"),
        ],
        [
            InlineKeyboardButton("Finance", callback_data="profile:niche:Finance"),
            InlineKeyboardButton("Business", callback_data="profile:niche:Business"),
        ],
        [
            InlineKeyboardButton("Lifestyle", callback_data="profile:niche:Lifestyle"),
            InlineKeyboardButton("Gaming", callback_data="profile:niche:Gaming"),
        ],
        [
            InlineKeyboardButton("Health", callback_data="profile:niche:Health"),
            InlineKeyboardButton("Education", callback_data="profile:niche:Education"),
        ],
    ])


def profile_tone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Bold & edgy", callback_data="profile:tone:Bold & edgy"),
            InlineKeyboardButton("Professional", callback_data="profile:tone:Professional"),
        ],
        [
            InlineKeyboardButton("Casual & fun", callback_data="profile:tone:Casual & fun"),
            InlineKeyboardButton("Informative", callback_data="profile:tone:Informative"),
        ],
        [
            InlineKeyboardButton("Analytical", callback_data="profile:tone:Analytical"),
            InlineKeyboardButton("Inspirational", callback_data="profile:tone:Inspirational"),
        ],
    ])


def profile_audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Beginners", callback_data="profile:audience:Beginners"),
            InlineKeyboardButton("Intermediate", callback_data="profile:audience:Intermediate"),
        ],
        [
            InlineKeyboardButton("Experts", callback_data="profile:audience:Experts"),
            InlineKeyboardButton("General", callback_data="profile:audience:General"),
        ],
    ])


def profile_style_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("News updates", callback_data="profile:style:News updates"),
            InlineKeyboardButton("Hot takes", callback_data="profile:style:Hot takes"),
        ],
        [
            InlineKeyboardButton("Tutorials", callback_data="profile:style:Tutorials"),
            InlineKeyboardButton("Memes & humor", callback_data="profile:style:Memes & humor"),
        ],
        [
            InlineKeyboardButton("Deep analysis", callback_data="profile:style:Deep analysis"),
            InlineKeyboardButton("Threads / series", callback_data="profile:style:Threads / series"),
        ],
    ])


def profile_reset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Reset profile", callback_data="profile:reset:confirm"),
        InlineKeyboardButton("❌ Keep current", callback_data="profile:reset:cancel"),
    ]])


# ─── v7: ENGAGE FORMAT SELECTION ─────────────────────────────────────────────

def engage_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗳 Poll", callback_data="engage:poll"),
            InlineKeyboardButton("⚔️ Debate", callback_data="engage:debate"),
        ],
        [
            InlineKeyboardButton("🔥 Hot Take", callback_data="engage:hot_take"),
            InlineKeyboardButton("🔮 Prediction", callback_data="engage:prediction"),
        ],
        [
            InlineKeyboardButton("✏️ Fill-in-blank", callback_data="engage:fill_blank"),
            InlineKeyboardButton("🎲 Surprise me", callback_data="engage:surprise"),
        ],
    ])


# ─── v7: BOOST ACTION ───────────────────────────────────────────────────────

def boost_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Apply improvements", callback_data="boost:apply")],
        [InlineKeyboardButton("❌ Cancel", callback_data="boost:cancel")],
    ])


# ─── v7: AUTOPILOT FREQUENCY ────────────────────────────────────────────────

def autopilot_frequency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 post/day", callback_data="autopilot:freq:1")],
        [InlineKeyboardButton("2 posts/day", callback_data="autopilot:freq:2")],
        [InlineKeyboardButton("3 posts/day", callback_data="autopilot:freq:3")],
    ])


def autopilot_mix_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔀 Mixed", callback_data="autopilot:mix:mixed"),
            InlineKeyboardButton("📝 Posts only", callback_data="autopilot:mix:posts"),
        ],
        [
            InlineKeyboardButton("🔥 Viral only", callback_data="autopilot:mix:viral"),
            InlineKeyboardButton("💡 Ideas only", callback_data="autopilot:mix:ideas"),
        ],
    ])
