# Context Discipline Rules
- read a file ONCE, whole; never re-read an unchanged file (you have it in context — scroll, don't re-fetch)
- prefer `grep -n` to locate, then read ONLY the needed section
- batch related reads into one command, not N small ones
- one WO phase per session; end session between phases (fresh context beats fat cached context)
- never paste large file bodies into your own replies/summaries
- at task end, report tokens/cost if the harness exposes them
