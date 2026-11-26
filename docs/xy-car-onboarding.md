# XY-CAR account and release onboarding

The XY-CAR module enables app-scoped account sessions and public installer
releases. Migration `0019_xy_car_app` creates its schema. The API startup
command runs `app-data register-applications` after the migrations.
Migration `0020_xy_car_station_data` adds private device settings and missions.
Migration `0021_xy_car_vehicle_connections` creates the account vehicle list
and copies existing device settings into it. Web and native clients use
`GET /v1/apps/xy_car_app/station/vehicles` to load the list and its revision.
Each write includes the revision used to merge local changes. On a `409`, the
client reloads the latest list and reapplies those changes. The server tracks
deleted IDs so stale edits cannot recreate a deleted vehicle.
PostgreSQL restricts station records to the signed-in XY-CAR user. The API
serves this data directly.

Prepare new private deployment input from the active API Compose. The Web
origin is `https://cznas.pp.ua:8090` and the NAS reverse proxy forwards to
`http://127.0.0.1:8089`. The origin tool adds the XY-CAR entry and its loopback
preview sources. Existing application entries, proxy trust and database mounts
are carried through the input.

```powershell
python tools/configure_xy_car_web.py --compose <active-compose> --output <with-xy-origins>
python tools/configure_public_releases.py --compose <with-xy-origins> --output <xy-build-input> --app-id xy_car_app --publisher-config <private-publisher-config>
```

The publisher config and generated key stay in the private runtime directory.
The API Compose contains the key digest and installer storage configuration.
After the API and Web gateway are deployed, verify the XY-CAR client-config
route, the browser session bootstrap, and anonymous release access before
allocating the first APK release batch. Use the existing API Compose project
and its PostgreSQL and PowerSync volumes.
