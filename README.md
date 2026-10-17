# garmin-datastore

Sync **all** of your Garmin Connect data into your own Supabase database, from a
worker on Vercel, and let Claude read it and write back to Garmin (log weight,
create and schedule workouts) through an MCP connector.

> **Status:** early development, milestone M0 (cloud spike). Not usable yet.

- [Requirements](docs/requirements.md)
- [Technical design](docs/design.md)

Built on the unofficial Garmin Connect API via
[`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect).
It's intended for personal use with your own account.

## License

[MIT](LICENSE)
