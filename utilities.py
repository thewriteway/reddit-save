import os
import re
from datetime import datetime

import praw
import requests
from redvid import Downloader
from requests.exceptions import RequestException, Timeout
import yt_dlp

from logindata import (REDDIT_PASSWORD, REDDIT_USERNAME, client_id,
                       client_secret)

IMAGE_EXTENSIONS = ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp', 'tiff','gifv']
VIDEO_EXTENSIONS = ['mp4', 'mkv', 'webm', 'flv', 'avi', 'mov']
PLATFORMS = ['youtube.com', 'vimeo.com', 'dailymotion.com','redgifs.com', 'gfycat.com', 'imgur.com']  # Add more platforms as needed

def make_client():
    """Creates a PRAW client with the details in the secrets.py file."""

    return praw.Reddit(client_id=client_id,
        client_secret=client_secret,
        user_agent="reddit-saver",
        username=REDDIT_USERNAME,
        password=REDDIT_PASSWORD )


def get_saved_posts(client):
    """Gets a list of posts that the user has saved."""

    return [
        saved for saved in client.user.me().saved(limit=None)
        if saved.__class__.__name__ == "Submission"
    ]


def get_upvoted_posts(client):
    """Gets a list of posts that the user has upvoted."""

    return [
        upvoted for upvoted in client.user.me().upvoted(limit=None)
        if upvoted.__class__.__name__ == "Submission"
    ]


def get_saved_comments(client):
    """Gets a list of comments that the user has saved."""

    return [
        saved for saved in client.user.me().saved(limit=None)
        if saved.__class__.__name__ != "Submission"
    ]


def get_post_html(post):
    """Takes a post object and creates a HTML for it - but not including the
    preview HTML."""

    with open(os.path.join("html", "post-div.html"), encoding="utf-8") as f:
        html = f.read()
    dt = datetime.utcfromtimestamp(post.created_utc)
    html = html.replace("<!--title-->", post.title)
    html = html.replace("<!--subreddit-->", f"/r/{str(post.subreddit)}")
    html = html.replace("<!--user-->", f"/u/{post.author.name}" if post.author else "[deleted]")
    html = html.replace("<!--link-->", f"posts/{post.id}.html")
    html = html.replace("<!--reddit-link-->", f"https://reddit.com{post.permalink}")
    html = html.replace("<!--content-link-->", post.url)
    html = html.replace("<!--id-->", post.id)
    html = html.replace("<!--body-->", (post.selftext_html or "").replace(
        '<a href="/r/', '<a href="https://reddit.com/r/'
    ))
    html = html.replace("<!--timestamp-->", str(dt))
    html = html.replace("<!--date-->", dt.strftime("%d %B, %Y"))
    return html


def save_media(post, location):
    """Takes a post object and tries to download any image/video it might be
    associated with. If it can, it will return the filename."""

    url = post.url
    stripped_url = url.split("?")[0]
    if url.endswith(post.permalink):
        return None

    extension = stripped_url.split(".")[-1].lower()
    domain = ".".join(post.url.split("/")[2].split(".")[-2:])
    readable_name = list(filter(bool, post.permalink.split("/")))[-1]

    # If it's an imgur gallery, forget it
    if domain == "imgur.com" and "gallery" in url:
        return None

    # Can the media be obtained directly?
    if extension in IMAGE_EXTENSIONS + VIDEO_EXTENSIONS:
        filename = f"{readable_name}_{post.id}.{extension}"
        try:
            response = requests.get(post.url, timeout=10)
            media_type = response.headers.get("Content-Type", "")
            if media_type.startswith("image") or media_type.startswith("video"):
                with open(os.path.join(location, "media", filename), "wb") as f:
                    f.write(response.content)
                return filename
        except (RequestException, Timeout) as e:
            print(f"Request failed: {e}")
            return None

    # Is this a v.redd.it link?
    if domain == "redd.it":
        downloader = Downloader(max_q=True, log=False)
        downloader.url = url
        current = os.getcwd()
        try:
            name = downloader.download()
            extension = name.split(".")[-1]
            filename = f"{readable_name}_{post.id}.{extension}"
            os.rename(name, os.path.join(location, "media", filename))
            return filename
        except Exception as e:
            print(f"v.redd.it download failed: {e}")
            os.chdir(current)
            return None

    # Is it a gfycat link that redirects? Update the URL if possible
    if domain == "gfycat.com":
        try:
            html = requests.get(post.url, timeout=10).content
            if len(html) < 50000:
                match = re.search(r"http([\dA-Za-z\+\:\/\.]+)\.mp4", html.decode())
                if match:
                    url = match.group()
                else:
                    return None
        except (RequestException, Timeout) as e:
            print(f"gfycat request failed: {e}")
            return None

    # Is this an imgur image?
    if domain == "imgur.com" and extension != "gifv":
        for ext in IMAGE_EXTENSIONS:
            direct_url = f'https://i.{url[url.find("//") + 2:]}.{ext}'
            direct_url = direct_url.replace("i.imgur.com", "imgur.com")
            direct_url = direct_url.replace("m.imgur.com", "imgur.com")
            try:
                response = requests.get(direct_url, timeout=10)
                if response.status_code == 200:
                    filename = f"{readable_name}_{post.id}.{ext}"
                    with open(os.path.join(location, "media", filename), "wb") as f:
                        f.write(response.content)
                    return filename
            except (RequestException, Timeout) as e:
                print(f"Imgur request failed: {e}")
                return None

    # Try to use yt-dlp if it's one of the possible domains
    if domain in PLATFORMS:
        options = {
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "outtmpl": os.path.join(
                location, "media", f"{readable_name}_{post.id}" + ".%(ext)s"
            ),
            "timeout": 10  # Set the timeout to 10 seconds
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
        except Exception as e:
            print(f"yt-dlp download failed: {e}")
            return None

        for f in os.listdir(os.path.join(location, "media")):
            if f.startswith(f"{readable_name}_{post.id}"):
                return f

    return None  # Return None if no media was found or downloaded


def add_media_preview_to_html(post_html, media):
    """Takes post HTML and returns a modified version with the preview
    inserted."""

    extension = media.split(".")[-1]
    location = "/".join(["media", media])
    if extension in IMAGE_EXTENSIONS:
        return post_html.replace(
            "<!--preview-->",
            f'<img src="{location}">'
        )
    if extension in VIDEO_EXTENSIONS:
        return post_html.replace(
            "<!--preview-->",
            f'<video controls><source src="{location}"></video>'
        )
    return post_html


def create_post_page_html(post, post_html):
    """Creates the HTML for a post's own page."""

    with open(os.path.join("html", "post.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("<!--title-->", post.title)
    html = html.replace("<!--post-->", post_html.replace("h2>", "h1>").replace(
        '<img src="media/', '<img src="../media/'
    ).replace(
        '<source src="media/', '<source src="../media/'
    ))
    html = re.sub(r'<a href="posts(.+?)</a>', "", html)
    with open(os.path.join("html", "style.css"), encoding="utf-8") as f:
        html = html.replace("<style></style>", f"<style>\n{f.read()}\n</style>")
    with open(os.path.join("html", "main.js"), encoding="utf-8") as f:
        html = html.replace("<script></script>", f"<script>\n{f.read()}\n</script>")
    comments_html = []
    post.comments.replace_more(limit=0)
    for comment in post.comments:
        comments_html.append(get_comment_html(
            comment, op=post.author.name if post.author else None
        ))
    html = html.replace("<!--comments-->", "\n".join(comments_html))
    return html


def get_comment_html(comment, children=True, op=None):
    """Takes a post object and creates a HTML for it - it will get its children
    too unless you specify otherwise."""

    with open(os.path.join("html", "comment-div.html"), encoding="utf-8") as f:
        html = f.read()
    dt = datetime.utcfromtimestamp(comment.created_utc)
    author = "[deleted]"
    if comment.author:
        if comment.author == op:
            author = f'<span class="op">/u/{comment.author.name}</span>'
        else:
            author = f"/u/{comment.author.name}"
    html = html.replace("<!--user-->", author)
    html = html.replace("<!--body-->", (comment.body_html or "").replace(
        '<a href="/r/', '<a href="https://reddit.com/r/'
    ))
    html = html.replace("<!--score-->", str(comment.score))
    html = html.replace("<!--link-->", f"https://reddit.com{comment.permalink}")
    html = html.replace("<!--timestamp-->", str(dt))
    html = html.replace("<!--id-->", comment.id)
    html = html.replace("<!--date-->", dt.strftime("%H:%M - %d %B, %Y"))
    if children:
        children_html = []
        for child in comment.replies:
            children_html.append(get_comment_html(child, children=False, op=op))
        html = html.replace("<!--children-->", "\n".join(children_html))
    return html
