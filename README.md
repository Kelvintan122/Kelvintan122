# RedNote (Xiaohongshu) GrabFood scraper

Collects RedNote posts about a keyword (default **GrabFood**), each post's comments, and the users who wrote them.

## Setup (on your own computer)
```bash
pip install -r requirements.txt
playwright install chromium
```

## Run
```bash
# Search by keyword (a browser opens; scan the QR code to log in the first time)
python rednote_scraper.py --keyword GrabFood --max-posts 50 --max-comments 300

# Other spellings / languages
python rednote_scraper.py --keyword "grab外卖"

# Specific post links (copy from the browser address bar, keep the xsec_token part)
python rednote_scraper.py --urls "https://www.xiaohongshu.com/explore/<id>?xsec_token=..."
python rednote_scraper.py --url-file links.txt --replies   # --replies also expands comment replies
```

## Output (`output/<timestamp>/`)
- `posts.csv`: note_id, url, title, description, tags, publish_time, ip_location, author, likes/collects/comments/shares
- `comments.csv`: comment_id, note_id, parent_comment_id (for replies), content, likes, ip_location, create_time, user_id, user_nickname, user_profile_url
- `raw.json`: the same data as JSON

## Notes
- Use a secondary RedNote account and keep volumes modest: RedNote rate-limits and may restrict accounts that scrape aggressively.
- Login is saved in `rednote_profile/`; after the first run you can add `--headless`.
- Commenter IDs and nicknames are personal data. Store them securely and use them only for analysis, in line with RedNote's terms and your local privacy law (e.g. Malaysia PDPA, Singapore PDPA).
