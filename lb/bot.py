# Letterboxd_Bot - A maubot plugin to subscribe to Letterboxd activity feeds
# Copyright (C) 2024 Adam Kessel
# Derived from rss_bot
# Copyright (C) 2022 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from typing import Any, Iterable
from datetime import datetime
from time import mktime, time
import asyncio
import hashlib
import magic
import re
import aiohttp
import feedparser
from io import BytesIO
from PIL import Image

from maubot import MessageEvent, Plugin, __version__ as maubot_version
from maubot.handlers import command, event
from mautrix.types import (
    EventID,
    EventType,
    MessageType,
    PowerLevelStateEventContent,
    MediaMessageEventContent,
    ImageInfo,
    RoomID,
    StateEvent,
)
from mautrix.util.async_db import UpgradeTable
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .db import DBManager, Entry, Feed, Subscription
from .migrations import upgrade_table

lb_change_level = EventType.find(
    "xyz.maubot.lb", t_class=EventType.Class.STATE)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("update_interval")
        helper.copy("max_backoff")
        helper.copy("spam_sleep")
        helper.copy("command_prefix")
        helper.copy("admins")


class BoolArgument(command.Argument):
    def __init__(self, name: str, label: str = None, *, required: bool = False) -> None:
        super().__init__(name, label, required=required, pass_raw=False)

    def match(self, val: str, **kwargs) -> tuple[str, Any]:
        part = val.split(" ")[0].lower()
        if part in ("f", "false", "n", "no", "0"):
            res = False
        elif part in ("t", "true", "y", "yes", "1"):
            res = True
        else:
            raise ValueError("invalid boolean")
        return val[len(part):], res


class LBBot(Plugin):
    dbm: DBManager
    poll_task: asyncio.Future
    http: aiohttp.ClientSession
    power_level_cache: dict[RoomID, tuple[int, PowerLevelStateEventContent]]

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table

    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()
        self.dbm = DBManager(self.database)
        self.http = self.client.api.session
        self.power_level_cache = {}
        self.poll_task = asyncio.create_task(self.poll_feeds())

    async def stop(self) -> None:
        await super().stop()
        self.poll_task.cancel()

    async def poll_feeds(self) -> None:
        try:
            await self._poll_feeds()
        except asyncio.CancelledError:
            self.log.debug("Polling stopped")
        except Exception:
            self.log.exception("Fatal error while polling feeds")

    async def _send(self, feed: Feed, entry: Entry, sub: Subscription) -> EventID:
        message = f"[{
            sub.user_id}](https://matrix.to/#/{sub.user_id}) rated [{entry.title}]({entry.link})."
        description = entry.description
        img = re.match(r'.*<img src=.(?P<url>[^"\']*)', description)
        image = None
        if img:
            image = await self._get_image(img['url'])
        msgtype = MessageType.NOTICE if sub.send_notice else MessageType.TEXT
        try:
            await self.client.send_markdown(
                sub.room_id, message, msgtype=msgtype, allow_html=True
            )
            if img and image:
                await self.client.send_message(
                    room_id=sub.room_id,
                    content=image
                )
            return True
        except Exception as e:
            self.log.warning(f"Failed to send {entry.id} of {
                             feed.id} to {sub.room_id}: {e}")

    async def _broadcast(
        self, feed: Feed, entry: Entry, subscriptions: list[Subscription]
    ) -> None:
        self.log.debug(f"Broadcasting {entry.id} of {feed.id}")
        spam_sleep = self.config["spam_sleep"]
        tasks = [self._send(feed, entry, sub) for sub in subscriptions]
        if spam_sleep >= 0:
            for task in tasks:
                await task
                await asyncio.sleep(spam_sleep)
        else:
            await asyncio.gather(*tasks)

    async def _poll_once(self) -> None:
        subs = await self.dbm.get_feeds()
        if not subs:
            return
        now = int(time())
        tasks = [self.try_parse_feed(feed=feed)
                 for feed in subs if feed.next_retry < now]
        feed: Feed
        entries: Iterable[Entry]
        self.log.info(f"Polling {len(tasks)} feeds")
        for res in asyncio.as_completed(tasks):
            feed, entries = await res
            self.log.debug(
                f"Fetching {feed.id} (backoff: {
                    feed.error_count} / {feed.next_retry}) "
                f"success: {bool(entries)}"
            )
            if not entries:
                error_count = feed.error_count + 1
                next_retry_delay = self.config["update_interval"] * \
                    60 * error_count
                next_retry_delay = min(
                    next_retry_delay, self.config["max_backoff"] * 60)
                next_retry = int(time() + next_retry_delay)
                self.log.debug(f"Setting backoff of {feed.id} to {
                               error_count} / {next_retry}")
                await self.dbm.set_backoff(feed, error_count, next_retry)
                continue
            elif feed.error_count > 0:
                self.log.debug(f"Resetting backoff of {feed.id}")
                await self.dbm.set_backoff(feed, error_count=0, next_retry=0)
            try:
                new_entries = {entry.id: entry for entry in entries}
            except Exception:
                self.log.exception(f"Weird error in items of {feed.url}")
                continue
            for old_entry in await self.dbm.get_entries(feed.id):
                new_entries.pop(old_entry.id, None)
            self.log.debug(f"Feed {feed.id} had {
                           len(new_entries)} new entries")
            new_entry_list: list[Entry] = list(new_entries.values())
            new_entry_list.sort(key=lambda entry: (entry.date, entry.id))
            await self.dbm.add_entries(new_entry_list)
            for entry in new_entry_list:
                await self._broadcast(feed, entry, feed.subscriptions)
        self.log.info(f"Finished polling {len(tasks)} feeds")

    async def _poll_feeds(self) -> None:
        self.log.debug("Polling started")
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                self.log.debug("Polling stopped")
            except Exception:
                self.log.exception("Error while polling feeds")
            await asyncio.sleep(self.config["update_interval"] * 60)

    async def try_parse_feed(self, feed: Feed | None = None) -> tuple[Feed, list[Entry]]:
        try:
            self.log.debug(
                f"Trying to fetch {feed.id} / {feed.url} "
                f"(backoff: {feed.error_count} / {feed.next_retry})"
            )
            return await self.parse_feed(feed=feed)
        except Exception as e:
            self.log.warning(f"Failed to parse feed {
                             feed.id} / {feed.url}: {e}")
            return feed, []

    @property
    def _feed_get_headers(self) -> dict[str, str]:
        return {
            "User-Agent": f"maubot/{maubot_version} +https://github.com/maubot/lb",
        }

    async def parse_feed(
        self, *, feed: Feed | None = None, url: str | None = None
    ) -> tuple[Feed, list[Entry]]:
        if feed is None:
            if url is None:
                raise ValueError("Either feed or url must be set")
            feed = Feed(id=-1, url=url, title="", link="")
        elif url is not None:
            raise ValueError("Only one of feed or url must be set")
        resp = await self.http.get(feed.url, headers=self._feed_get_headers)
        return await self._parse_lb(feed, resp)

    async def _get_image(self, image_url: str) -> None:
        try:
            resp = await self.http.get(image_url)
            if resp.status == 200:
                data = await resp.read()
                mime_type = magic.from_buffer(data, mime=True)
                image = Image.open(BytesIO(data))
                (w, h) = image.size
                thumbx = int((w/h)*200)
                thumby = 200
                uri = await self.client.upload_media(data, mime_type=mime_type)
                if (uri):
                    buf = BytesIO()
                    thumb = image.resize((thumbx, thumby))
                    rgb_im = thumb.convert('RGB')
                    rgb_im.save(buf, format='JPEG')
                    thumburi = await self.client.upload_media(buf.getvalue(), mime_type='image/jpeg')
                    if (thumburi):
                        content = MediaMessageEventContent(
                            msgtype=MessageType.IMAGE,
                            url=uri,
                            body=f"image.jpg",
                            info=ImageInfo(mimetype='image/jpeg',
                                           height=h,
                                           width=w,
                                           size=len(data),
                                           thumbnail_url=thumburi
                                           )
                        )
                        return content
            else:
                self.log.error(f"Getting media info for {image_url} returned {resp.status}: "
                               f"{await resp.text()}")
                return None
        except Exception as e:
            self.log.debug(
                f'Failed to get image {image_url}: {e}')
            return None

    @classmethod
    async def _parse_json(
        cls, feed: Feed, resp: aiohttp.ClientResponse
    ) -> tuple[Feed, list[Entry]]:
        content = await resp.json()
        if content["version"] not in (
            "https://jsonfeed.org/version/1",
            "https://jsonfeed.org/version/1.1",
        ):
            raise ValueError("Unsupported JSON feed version")
        if not isinstance(content["items"], list):
            raise ValueError(
                "Feed is not a valid JSON feed (items is not a list)")
        feed.title = content["title"]
        feed.link = content.get("link", "")
        return feed, [cls._parse_json_entry(feed.id, entry) for entry in content["items"]]

    @classmethod
    def _parse_json_entry(cls, feed_id: int, entry: dict[str, Any]) -> Entry:
        try:
            date = datetime.fromisoformat(entry["date_published"])
        except (ValueError, KeyError):
            date = datetime.now()
        title = entry.get("title", "")
        entry_id = str(entry["id"])
        link = entry.get("url") or entry_id
        description = entry.get("description") or ""
        return Entry(feed_id=feed_id, id=entry_id, date=date, title=title, description=description, link=link)

    @classmethod
    async def _parse_lb(
        cls, feed: Feed, resp: aiohttp.ClientResponse
    ) -> tuple[Feed, list[Entry]]:
        try:
            content = await resp.text()
        except UnicodeDecodeError:
            try:
                content = await resp.text(encoding="utf-8", errors="ignore")
            except UnicodeDecodeError:
                content = str(await resp.read())[2:-1]
        headers = {"Content-Location": feed.url, **
                   resp.headers, "Content-Encoding": "identity"}
        content=content.strip()
        parsed_data = feedparser.parse(content, response_headers=headers)
        if parsed_data.bozo:
            if not isinstance(parsed_data.bozo_exception, feedparser.ThingsNobodyCaresAboutButMe):
                raise parsed_data.bozo_exception
        feed_data = parsed_data.get("feed", {})
        feed.title = feed_data.get("title", feed.link)
        feed.link = feed_data.get("link", "")
        return feed, [cls._parse_lb_entry(feed.id, entry) for entry in parsed_data.entries]

    @classmethod
    def _parse_lb_entry(cls, feed_id: int, entry: Any) -> Entry:
        return Entry(
            feed_id=feed_id,
            id=(
                getattr(entry, "id", None)
                or getattr(entry, "link", None)
                or hashlib.sha1(
                    " ".join(
                        [
                            getattr(entry, "title", ""),
                            getattr(entry, "description", ""),
                        ]
                    ).encode("utf-8")
                ).hexdigest()
            ),
            date=cls._parse_lb_date(entry),
            title=getattr(entry, "title", ""),
            description=getattr(entry, "description", "").strip(),
            link=getattr(entry, "link", ""),
        )

    @staticmethod
    def _parse_lb_date(entry: Any) -> datetime:
        try:
            return datetime.fromtimestamp(mktime(entry["published_parsed"]))
        except (KeyError, TypeError, ValueError):
            pass
        try:
            return datetime.fromtimestamp(mktime(entry["date_parsed"]))
        except (KeyError, TypeError, ValueError):
            pass
        return datetime.now()

    async def get_power_levels(self, room_id: RoomID) -> PowerLevelStateEventContent:
        try:
            expiry, levels = self.power_level_cache[room_id]
            if expiry < int(time()):
                return levels
        except KeyError:
            pass
        levels = await self.client.get_state_event(room_id, EventType.ROOM_POWER_LEVELS)
        self.power_level_cache[room_id] = (int(time()) + 5 * 60, levels)
        return levels

    async def can_manage(self, evt: MessageEvent) -> bool:
        if evt.sender in self.config["admins"]:
            return True
        levels = await self.get_power_levels(evt.room_id)
        user_level = levels.get_user_level(evt.sender)
        state_level = levels.get_event_level(lb_change_level)
        if not isinstance(state_level, int):
            state_level = 50
        if user_level < state_level:
            await evt.reply(
                "You don't have the permission to manage the subscriptions of this room."
            )
            return False
        return True

    @command.new(
        name="lb",
        aliases=["letterboxd"],
        help="Manage this Letterboxd bot",
        require_subcommand=True,
    )
    async def lb(self) -> None:
        pass

    @lb.subcommand(
        "subscribe", aliases=("s", "sub"),
        help="Subscribe this room to a Letterboxd feed with the given username."
    )
    @command.argument("url", "Letterboxd username", pass_raw=True)
    async def subscribe(self, evt: MessageEvent, url: str) -> None:
        if not await self.can_manage(evt):
            return
        if not 'letterboxd' in url:
            url = f'https://letterboxd.com/{url}/rss/'
        feed = await self.dbm.get_feed_by_url(url)
        if not feed:
            try:
                info, entries = await self.parse_feed(url=url)
            except Exception as e:
                await evt.reply(f"Failed to load feed: {e}")
                return
            feed = await self.dbm.create_feed(info)
            await self.dbm.add_entries(entries, override_feed_id=feed.id)
        elif feed.error_count > 0:
            await self.dbm.set_backoff(feed, error_count=feed.error_count, next_retry=0)
        feed_info = f"feed ID {feed.id}: [{feed.title}]({feed.url})"
        sub, _ = await self.dbm.get_subscription(feed.id, evt.room_id)
        if sub is not None:
            subscriber = (
                "You"
                if sub.user_id == evt.sender
                else f"[{sub.user_id}](https://matrix.to/#/{sub.user_id})"
            )
            await evt.reply(f"{subscriber} had already subscribed this room to {feed_info}")
        else:
            await self.dbm.subscribe(
                feed.id, evt.room_id, evt.sender
            )
            await evt.reply(f"Subscribed to {feed_info}")

    @lb.subcommand(
        "unsubscribe", aliases=("u", "unsub"), help="Subscribe this room to the given feed ID."
    )
    @command.argument("feed_id", "feed ID", parser=int)
    async def unsubscribe(self, evt: MessageEvent, feed_id: int) -> None:
        if not await self.can_manage(evt):
            return
        sub, feed = await self.dbm.get_subscription(feed_id, evt.room_id)
        if not sub:
            await evt.reply("This room is not subscribed to that feed")
            return
        await self.dbm.unsubscribe(feed.id, evt.room_id)
        await evt.reply(f"Unsubscribed from feed ID {feed.id}: [{feed.title}]({feed.url})")

    @lb.subcommand(
        "poll", aliases=("p"), help="Poll for new entries."
    )
    async def poll(self, evt: MessageEvent) -> None:
        if not await self.can_manage(evt):
            return
        await self.poll_feeds()

    @lb.subcommand(
        "notice", aliases=("n",), help="Set whether or not the bot should send updates as m.notice"
    )
    @command.argument("feed_id", "feed ID", parser=int)
    @BoolArgument("setting", "true/false")
    async def command_notice(self, evt: MessageEvent, feed_id: int, setting: bool) -> None:
        if not await self.can_manage(evt):
            return
        sub, feed = await self.dbm.get_subscription(feed_id, evt.room_id)
        if not sub:
            await evt.reply("This room is not subscribed to that feed")
            return
        await self.dbm.set_send_notice(feed.id, evt.room_id, setting)
        send_type = "m.notice" if setting else "m.text"
        await evt.reply(f"Updates for feed ID {feed.id} will now be sent as `{send_type}`")

    @staticmethod
    def _format_subscription(feed: Feed, subscriber: str) -> str:
        msg = (
            f"Feed {feed.id}: [{
                subscriber}](https://matrix.to/#/{subscriber}) is subscribed to [{feed.title}]({feed.url})."
        )
        if feed.error_count > 1:
            msg += f"  \n  ⚠️ The last {
                feed.error_count} attempts to fetch the feed have failed!"
        return msg

    @lb.subcommand(
        "subscriptions",
        aliases=("ls", "list", "subs"),
        help="List the subscriptions in the current room.",
    )
    async def command_subscriptions(self, evt: MessageEvent) -> None:
        subscriptions = await self.dbm.get_feeds_by_room(evt.room_id)
        if len(subscriptions) == 0:
            await evt.reply("There are no Letterboxd subscriptions in this room")
            return
        await evt.reply(
            "**Subscriptions in this room:**\n\n"
            + "\n".join(
                self._format_subscription(feed, subscriber) for feed, subscriber in subscriptions
            )
        )

    @event.on(EventType.ROOM_TOMBSTONE)
    async def tombstone(self, evt: StateEvent) -> None:
        if not evt.content.replacement_room:
            return
        await self.dbm.update_room_id(evt.room_id, evt.content.replacement_room)
