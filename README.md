# Lettrerboxd Bot
A [maubot](https://github.com/maubot/maubot) that posts film ratings from [Letterboxd](https://letterboxd.com).

Once you are subscribed, any new ratings will be posted in subscribed rooms along with cover art for the film.

## Usage
Basic commands:

* `!lb subscribe <username>` - Subscribe to a Letterboxd feed for the given username.
* `!lb unsubscribe <feed ID>` - Unsubscribe the Letterboxd feed with the given ID.
* `!lb subscriptions` - List subscriptions (and feed IDs) in the current room.
* `!lb notice <feed ID> [true/false]` - Set whether the bot should send new
  posts as `m.notice` (if false, they're sent as `m.text`).
