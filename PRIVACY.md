# Privacy Policy — Channel Lens

_Last updated: 3 September 2026_

Channel Lens is a personal, open-source desktop application. It runs entirely on
the computer it is installed on. There is no Channel Lens server, no account, and
no operator who can see your data.

## What is collected

**Nothing is collected.** The developer of this application receives no data of
any kind from it — no analytics, no telemetry, no crash reports, no usage
statistics. The application makes no network requests to any server operated by
the developer, because no such server exists.

## What the application stores, and where

All data stays in a single folder on your own computer:

- Windows: `%LOCALAPPDATA%\channel-lens`
- macOS / Linux: `~/.local/share/channel-lens`

That folder contains a local SQLite database (public YouTube video and channel
information the application has fetched, plus any analytics for your own
channel), your settings, cached thumbnail images, and your API credentials.

Nothing in that folder is transmitted anywhere. Deleting the folder erases
everything the application holds.

## Google user data

If you connect a Google account, the application requests a single read-only
scope:

`https://www.googleapis.com/auth/yt-analytics.readonly`

This permits reading the YouTube Analytics and YouTube Reporting data for
channels you own — views, watch time, audience retention, thumbnail impressions,
and click-through rate.

That data is requested directly by the application running on your computer,
from Google's servers, and written to the local database described above. It is
used only to display your own channel's performance to you, inside the
application. It is never transmitted to the developer or to any third party, and
it is not used for advertising, sold, or shared with anyone.

The application requests no write access. It cannot upload, edit, or delete
anything on your channel. It does not request revenue or monetary scopes.

Your OAuth token is stored in the local folder above and is used only to
authenticate your requests to Google.

Channel Lens's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Optional third-party processing

If — and only if — you supply your own Anthropic API key, the application can
send **publicly available YouTube thumbnail images** to the Anthropic API to
generate a written description of them. This feature is off unless you configure
a key. No personal information, no analytics data, and no credentials are
included in those requests. See Anthropic's
[privacy policy](https://www.anthropic.com/legal/privacy).

No other third party receives anything.

## Retention and deletion

Data is kept locally until you delete it. You can remove everything by deleting
the folder named above, or disconnect Google access at any time from within the
application (Settings → Disconnect), which deletes the stored token.

You can also revoke the application's access to your Google account at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Children

This application is not directed to children under 13.

## Changes

Any change to this policy will be published in this file in the project
repository, with the date above updated.

## Contact

Raise an issue on the project repository.
