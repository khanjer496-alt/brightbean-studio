# Platform API versions to rotate

Some platforms retire the API version we send about a year after releasing it. When one does, every
call to that platform fails at once. This page lists what is pinned, where, and how to move it.

| Platform | Pinned where | Current | Retired after | Check the next version at |
| --- | --- | --- | --- | --- |
| LinkedIn | `LINKEDIN_API_VERSION` environment setting (default in `providers/linkedin.py`, `DEFAULT_LINKEDIN_API_VERSION`) | `202604` | about April 2027 (a version is supported for at least one year) | https://learn.microsoft.com/en-us/linkedin/marketing/versioning |

Put a calendar reminder at month 10 of each version (for `202604`, February 2027).

## LinkedIn: moving to a new version

1. Read the changelog between the pinned version and the newest one:
   https://learn.microsoft.com/en-us/linkedin/marketing/integrations/recent-changes
   The Posts API (`/rest/posts`), Images, Videos, Documents and `socialActions` are what we call.
2. Set `LINKEDIN_API_VERSION` to the new `YYYYMM` value in the production environment and restart the
   web and worker processes. A malformed value stops the process at start-up, on purpose.
3. Publish a text post, an image post and a document post from a test LinkedIn account (personal and
   company page) before restoring traffic.
4. Once it works, change `DEFAULT_LINKEDIN_API_VERSION` in `providers/linkedin.py` in a normal PR so the
   code and the environment agree again.

## What happens when a version is retired

LinkedIn answers `426` with `code: NONEXISTENT_VERSION` ("Requested version ... is not active").

* The request fails with `ApiVersionSunsetError`: permanent for the post (no retries), with the
  sentence "LinkedIn has retired the API version this app uses ... retry the post once we have."
* `providers.api_versions.report_version_sunset` logs at CRITICAL and notifies every active staff user
  (`PLATFORM_API_ALERT` in-app notification and email), at most once every six hours per version.
* After step 2 above, affected posts are retried from the post's page.

Other platforms to watch, with no 426 equivalent: the Meta Graph API version (an old version answers
with an OAuth/400 error), Hashnode's GraphQL schema, and TikTok/Pinterest API v-numbers.
