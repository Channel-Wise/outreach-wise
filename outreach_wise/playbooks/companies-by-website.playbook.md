+++
name = "Companies by website"
language = "en"

[input]
column = "website"
sources = ["web_page"]

[fields.hook]
describe = "1-2 sentences on one specific thing from their website (a product, a recent launch, a customer they mention) and how it relates to what we offer. No flattery."
max_words = 45

[fields.subject]
describe = "A short, specific subject line tied to the hook."
max_words = 8
alternatives = 3

[checks]
email_words = [50, 120]
subject_words = [3, 8]
banned = ["quick question", "hope this finds you well", "synergy", "leverage"]
one_link = true
numbers_in_data = true
+++

# Who you are writing as

Replace this section: who you are, what you sell, who it is for, and the single call to action of the email.

# Rules

- Only mention things that are on the fetched page. Never invent customers, numbers or quotes.
- One observation is enough. Don't summarize their whole website.
- If the page is empty, a parking page, or clearly unrelated to a business, set skip_reason.
- If you are unsure about something, leave it out and say so in notes_for_reviewer.
