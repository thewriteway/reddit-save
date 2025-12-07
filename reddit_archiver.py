#!/usr/bin/env python

import argparse
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import praw
import requests
import yt_dlp
from redvid import Downloader
from requests.exceptions import RequestException, Timeout
from tqdm import tqdm

from logindata import (REDDIT_PASSWORD, REDDIT_USERNAME, client_id,
                       client_secret)

# Constants
IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'bmp', 'gif', 'webp', 'tiff', 'gifv'}
VIDEO_EXTENSIONS = {'mp4', 'mkv', 'webm', 'flv', 'avi', 'mov'}
PLATFORMS = {'youtube.com', 'vimeo.com', 'dailymotion.com', 'redgifs.com', 
             'gfycat.com', 'imgur.com'}
MAX_FILENAME_LENGTH = 160
REQUEST_TIMEOUT = 10


class RedditArchiver:
    
    def __init__(self, location):
        self.location = Path(location)
        self.client = self._make_client()
        self._html_cache = {}
        self._setup_directories()
    
    @staticmethod
    def _make_client():
        """Creates a PRAW client with authentication."""
        return praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="reddit-saver",
            username=REDDIT_USERNAME,
            password=REDDIT_PASSWORD
        )
    
    def _setup_directories(self):
        """Create necessary directories if they don't exist."""
        (self.location / "media").mkdir(parents=True, exist_ok=True)
        (self.location / "posts").mkdir(parents=True, exist_ok=True)
    
    @lru_cache(maxsize=8)
    def _read_template(self, template_name):
        """Read and cache HTML templates."""
        template_path = Path("html") / template_name
        return template_path.read_text(encoding="utf-8")
    
    def get_posts(self, mode):
        """Get saved or upvoted posts."""
        user = self.client.user.me()
        source = user.saved(limit=None) if mode == "saved" else user.upvoted(limit=None)
        return [item for item in source if item.__class__.__name__ == "Submission"]
    
    def get_comments(self, mode):
        """Get saved comments (only for saved mode)."""
        if mode != "saved":
            return []
        return [item for item in self.client.user.me().saved(limit=None)
                if item.__class__.__name__ != "Submission"]
    
    def _replace_html_placeholders(self, html, replacements):
        """Replace multiple placeholders in HTML efficiently."""
        for placeholder, value in replacements.items():
            html = html.replace(placeholder, str(value))
        return html
    
    def get_post_html(self, post):
        """Generate HTML for a post."""
        template = self._read_template("post-div.html")
        dt = datetime.utcfromtimestamp(post.created_utc)
        
        replacements = {
            "<!--title-->": post.title,
            "<!--subreddit-->": f"/r/{post.subreddit}",
            "<!--user-->": f"/u/{post.author.name}" if post.author else "[deleted]",
            "<!--link-->": f"posts/{post.id}.html",
            "<!--reddit-link-->": f"https://reddit.com{post.permalink}",
            "<!--content-link-->": post.url,
            "<!--id-->": post.id,
            "<!--body-->": (post.selftext_html or "").replace(
                '<a href="/r/', '<a href="https://reddit.com/r/'),
            "<!--timestamp-->": str(dt),
            "<!--date-->": dt.strftime("%d %B, %Y")
        }
        
        return self._replace_html_placeholders(template, replacements)
    
    def save_media(self, post):
        """Download media associated with a post."""
        url = post.url
        
        # Skip if URL is just the permalink
        if url.endswith(post.permalink):
            return None
        
        stripped_url = url.split("?")[0]
        extension = stripped_url.split(".")[-1].lower()
        domain = ".".join(url.split("/")[2].split(".")[-2:])
        readable_name = [part for part in post.permalink.split("/") if part][-1]
        
        # Skip imgur galleries
        if domain == "imgur.com" and "gallery" in url:
            return None
        
        # Try direct download for images/videos
        if extension in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            return self._download_direct_media(post, readable_name, extension)
        
        # Handle v.redd.it
        if domain == "redd.it":
            return self._download_vreddit(url, readable_name, post.id)
        
        # Handle gfycat
        if domain == "gfycat.com":
            url = self._resolve_gfycat_url(url)
            if not url:
                return None
        
        # Handle imgur images
        if domain == "imgur.com" and extension != "gifv":
            return self._download_imgur(url, readable_name, post.id)
        
        # Try yt-dlp for supported platforms
        if domain in PLATFORMS:
            return self._download_with_ytdlp(url, readable_name, post.id)
        
        return None
    
    def _download_direct_media(self, post, readable_name, extension):
        """Download media directly from URL."""
        filename = f"{readable_name}_{post.id}.{extension}"
        try:
            response = requests.get(post.url, timeout=REQUEST_TIMEOUT)
            media_type = response.headers.get("Content-Type", "")
            
            if media_type.startswith(("image", "video")):
                (self.location / "media" / filename).write_bytes(response.content)
                return filename
        except (RequestException, Timeout) as e:
            print(f"Direct download failed: {e}")
        
        return None
    
    def _download_vreddit(self, url, readable_name, post_id):
        """Download v.redd.it video."""
        downloader = Downloader(max_q=True, log=False)
        downloader.url = url
        current_dir = os.getcwd()
        
        try:
            downloaded_name = downloader.download()
            extension = downloaded_name.split(".")[-1]
            filename = f"{readable_name}_{post_id}.{extension}"
            
            os.rename(downloaded_name, self.location / "media" / filename)
            return filename
        except Exception as e:
            print(f"v.redd.it download failed: {e}")
            os.chdir(current_dir)
        
        return None
    
    def _resolve_gfycat_url(self, url):
        """Resolve gfycat URL to direct video link."""
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if len(response.content) < 50000:
                match = re.search(r"http([\dA-Za-z\+\:\/\.]+)\.mp4", response.text)
                if match:
                    return match.group()
        except (RequestException, Timeout) as e:
            print(f"Gfycat resolution failed: {e}")
        
        return None
    
    def _download_imgur(self, url, readable_name, post_id):
        """Download imgur image."""
        base_url = url[url.find("//") + 2:]
        base_url = base_url.replace("i.imgur.com", "imgur.com")
        base_url = base_url.replace("m.imgur.com", "imgur.com")
        
        for ext in IMAGE_EXTENSIONS:
            direct_url = f"https://i.{base_url}.{ext}"
            try:
                response = requests.get(direct_url, timeout=REQUEST_TIMEOUT)
                if response.status_code == 200:
                    filename = f"{readable_name}_{post_id}.{ext}"
                    (self.location / "media" / filename).write_bytes(response.content)
                    return filename
            except (RequestException, Timeout):
                continue
        
        return None
    
    def _download_with_ytdlp(self, url, readable_name, post_id):
        """Download media using yt-dlp."""
        options = {
            "nocheckcertificate": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "outtmpl": str(self.location / "media" / f"{readable_name}_{post_id}.%(ext)s"),
            "timeout": REQUEST_TIMEOUT
        }
        
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
            
            # Find the downloaded file
            for file in (self.location / "media").iterdir():
                if file.name.startswith(f"{readable_name}_{post_id}"):
                    return file.name
        except Exception as e:
            print(f"yt-dlp download failed: {e}")
        
        return None
    
    def add_media_preview_to_html(self, post_html, media):
        """Add media preview to post HTML."""
        extension = media.split(".")[-1]
        location = f"media/{media}"
        
        if extension in IMAGE_EXTENSIONS:
            preview = f'<img src="{location}">'
        elif extension in VIDEO_EXTENSIONS:
            preview = f'<video controls><source src="{location}"></video>'
        else:
            return post_html
        
        return post_html.replace("<!--preview-->", preview)
    
    def create_post_page_html(self, post, post_html):
        """Create full HTML page for a post."""
        template = self._read_template("post.html")
        style = self._read_template("style.css")
        script = self._read_template("main.js")
        
        # Adjust paths and tags for individual post page
        adjusted_html = post_html.replace("h2>", "h1>")
        adjusted_html = adjusted_html.replace('<img src="media/', '<img src="../media/')
        adjusted_html = adjusted_html.replace('<source src="media/', '<source src="../media/')
        adjusted_html = re.sub(r'<a href="posts.+?</a>', "", adjusted_html)
        
        # Get comments
        post.comments.replace_more(limit=0)
        comments_html = [
            self.get_comment_html(comment, op=post.author.name if post.author else None)
            for comment in post.comments
        ]
        
        replacements = {
            "<!--title-->": post.title,
            "<!--post-->": adjusted_html,
            "<style></style>": f"<style>\n{style}\n</style>",
            "<script></script>": f"<script>\n{script}\n</script>",
            "<!--comments-->": "\n".join(comments_html)
        }
        
        return self._replace_html_placeholders(template, replacements)
    
    def get_comment_html(self, comment, children=True, op=None):
        """Generate HTML for a comment."""
        template = self._read_template("comment-div.html")
        dt = datetime.utcfromtimestamp(comment.created_utc)
        
        # Format author display
        if comment.author:
            author = (f'<span class="op">/u/{comment.author.name}</span>' 
                     if comment.author.name == op 
                     else f"/u/{comment.author.name}")
        else:
            author = "[deleted]"
        
        replacements = {
            "<!--user-->": author,
            "<!--body-->": (comment.body_html or "").replace(
                '<a href="/r/', '<a href="https://reddit.com/r/'),
            "<!--score-->": str(comment.score),
            "<!--link-->": f"https://reddit.com{comment.permalink}",
            "<!--timestamp-->": str(dt),
            "<!--id-->": comment.id,
            "<!--date-->": dt.strftime("%H:%M - %d %B, %Y")
        }
        
        html = self._replace_html_placeholders(template, replacements)
        
        # Add child comments
        if children:
            children_html = [
                self.get_comment_html(child, children=False, op=op)
                for child in comment.replies
            ]
            html = html.replace("<!--children-->", "\n".join(children_html))
        
        return html
    
    def _get_existing_items(self, html_file, item_class):
        """Extract existing post/comment IDs and HTML from archive."""
        file_path = self.location / html_file
        
        if not file_path.exists():
            return [], []
        
        content = file_path.read_text(encoding="utf-8")
        ids = re.findall(r'id="(.+?)"', content)
        items_html = re.findall(
            rf'(<div class="{item_class}"[\S\n\t\v ]+?<!--{item_class}end--><\/div>)',
            content
        )
        
        return ids, items_html
    
    def _sanitize_filename(self, subreddit, title):
        """Create a sanitized filename from subreddit and title."""
        # Combine subreddit and title
        filename = f"{subreddit}_{title}"
        
        # Remove invalid characters for Windows/Unix
        # Keep only alphanumeric, spaces, hyphens, and underscores
        filename = re.sub(r'[^\w\s-]', '', filename)
        
        # Replace spaces with underscores
        filename = filename.replace(' ', '_')
        
        # Replace multiple underscores/hyphens with single ones
        filename = re.sub(r'[-_]+', '_', filename)
        
        # Remove leading/trailing underscores, hyphens, or periods
        filename = filename.strip('_-.')
        
        # Ensure filename isn't empty
        if not filename:
            filename = "untitled"
        
        # Truncate to max length
        filename = filename[:MAX_FILENAME_LENGTH]
        
        # Remove trailing periods or spaces (Windows doesn't allow these)
        filename = filename.rstrip('. ')
        
        # Handle reserved Windows filenames
        reserved = {'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4',
                   'COM5', 'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2',
                   'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}
        if filename.upper() in reserved:
            filename = f"post_{filename}"
        
        return filename
    
    def archive(self, mode):
        """Main archiving function."""
        html_file = f"{mode}.html"
        
        # Get existing items
        existing_post_ids, existing_posts_html = self._get_existing_items(html_file, "post")
        existing_comment_ids, existing_comments_html = self._get_existing_items(html_file, "comment")
        
        # Process new posts
        all_posts = self.get_posts(mode)
        new_posts = [p for p in all_posts if p.id not in existing_post_ids]
        posts_html = []
        
        if new_posts:
            print(f"Processing {len(new_posts)} new posts...")
            for post in tqdm(new_posts):
                post_html = self.get_post_html(post)
                
                # Download and add media
                media = self.save_media(post)
                if media:
                    post_html = self.add_media_preview_to_html(post_html, media)
                
                posts_html.append(post_html)
                
                # Create individual post page
                try:
                    page_html = self.create_post_page_html(post, post_html)
                    postfile = self._sanitize_filename(str(post.subreddit), post.title)
                    post_path = self.location / "posts" / f"{postfile}.html"
                    
                    # Check if file already exists
                    if not post_path.exists():
                        post_path.write_text(page_html, encoding="utf-8")
                    else:
                        # If filename collision, append post ID
                        post_path = self.location / "posts" / f"{postfile}_{post.id}.html"
                        post_path.write_text(page_html, encoding="utf-8")
                except Exception as e:
                    print(f"Failed to create post page for '{post.title}': {e}")
                    # Continue processing other posts
        else:
            print("No new posts")
        
        posts_html.extend(existing_posts_html)
        
        # Process new comments
        all_comments = self.get_comments(mode)
        new_comments = [c for c in all_comments if c.id not in existing_comment_ids]
        comments_html = []
        
        if new_comments:
            print(f"Processing {len(new_comments)} new comments...")
            for comment in tqdm(new_comments):
                comments_html.append(self.get_comment_html(comment))
        else:
            print("No new comments")
        
        comments_html.extend(existing_comments_html)
        
        # Generate final HTML
        template = self._read_template(html_file)
        style = self._read_template("style.css")
        script = self._read_template("main.js")
        
        final_html = self._replace_html_placeholders(template, {
            "<style></style>": f"<style>\n{style}\n</style>",
            "<script></script>": f"<script>\n{script}\n</script>",
            "<!--posts-->": "\n".join(posts_html),
            "<!--comments-->": "\n".join(comments_html)
        })
        
        (self.location / html_file).write_text(final_html, encoding="utf-8")
        print(f"Archive saved to {self.location / html_file}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Archive Reddit posts and comments.")
    parser.add_argument(
        "mode", 
        choices=["saved", "upvoted"],
        help="Archive saved or upvoted posts"
    )
    
    if os.getenv("DOCKER", "0") != "1":
        parser.add_argument("location", help="Directory path to save archive")
    
    args = parser.parse_args()
    
    # Determine location
    location = "./archive/" if os.getenv("DOCKER", "0") == "1" else args.location
    
    # Validate location
    if not os.path.isdir(location):
        print(f"Error: {location} is not a directory")
        return 1
    
    # Run archiver
    archiver = RedditArchiver(location)
    archiver.archive(args.mode)
    
    return 0


if __name__ == "__main__":
    exit(main())
