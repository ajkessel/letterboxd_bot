# rss
A [maubot](https://github.com/maubot/maubot) that posts movie ratings from [Letterboxd](https://letterboxd.com).

## Usage
Basic commands:

* `!lb subscribe <username>` - Subscribe to a Letterboxd feed for the given username.
* `!lb unsubscribe <feed ID>` - Unsubscribe the Letterboxd feed with the given ID.
* `!lb subscriptions` - List subscriptions (and feed IDs) in the current room.
* `!lb notice <feed ID> [true/false]` - Set whether the bot should send new
  posts as `m.notice` (if false, they're sent as `m.text`).
