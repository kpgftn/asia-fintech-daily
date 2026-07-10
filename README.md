# Asia Fintech Daily

One static news page per requirement sheet. `configs/<person>.yml` transcribes a
person's filled-in AI-wishlist requirement sheet (jurisdictions, topics,
regulators, preferences); `build.py` pulls Google News RSS + direct feeds,
keyword-tags topics (no LLM — raw links by design), and renders
`docs/<person>/index.html` plus a dated archive.

GitHub Actions rebuilds every morning ~6:15am SGT; GitHub Pages serves `docs/`.
No servers, no accounts, no database. Adding a person = adding one config file.

Local run: `pip install feedparser pyyaml && python build.py`
