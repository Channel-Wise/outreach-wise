+++
name = "YouTube creators"
language = "auto"

[input]
column = "channel_url"
sources = ["youtube_channel"]

[fields.greeting]
describe = "\"Hi <first name>,\" only if the creator's first name appears on the channel (About text, channel name or a video title). Otherwise leave it empty."
max_words = 4
fallback = "Hi there,"

[fields.hook]
describe = "1-3 short sentences about one concrete, verifiable thing on the channel: a specific video, a series, or a decision the creator explains. Name the video. No flattery, no generic praise."
max_words = 60

[fields.why_you]
describe = "One sentence on why this creator's view on what we offer would be valuable, based only on facts from the channel."
max_words = 45

[fields.subject]
describe = "A personal subject line tied to the hook. A short question about their own decision works well."
max_words = 8
alternatives = 3

[checks]
email_words = [60, 150]
subject_words = [3, 8]
banned = ["quick question", "hope this finds you well", "love your content", "synergy", "game-changer"]
one_link = true
numbers_in_data = true
+++

# Who you are writing as

Replace this section: who you are, what you built, and the one thing you are asking for (for example a short call or feedback on a product).

# Rules

- The hook must be about something you can see in the research data: a video title, a description, view counts, upload dates. Never invent quotes, numbers or titles.
- Prefer a video that is a few months old over the latest upload, and prefer videos where the creator explains a decision (a new format, a series, a break, a viewer request).
- Don't use sponsored videos as the hook. Stay away from drama, personal life, money and income.
- Don't claim to know why a video did well or badly, and don't promise results.
- Write in the channel's language.
- If the channel looks inactive, belongs to a minor, or gives financial or medical advice, set skip_reason instead of writing the fields.
- If you are unsure about something, leave it out and say so in notes_for_reviewer.
