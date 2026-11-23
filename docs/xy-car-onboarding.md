# XY-CAR account and release onboarding

The XY-CAR module enables app-scoped account sessions and public installer
releases. Migration `0019_xy_car_app` creates its schema. The API startup
command runs `app-data register-applications` after the migrations.
Migration `0020_xy_car_station_data` adds private device settings and missions.
The authenticated `/v1/apps/xy_car_app/station` routes expose `/device`,
`/mission`, and `/missions`. PostgreSQL restricts those records to the signed-in
XY-CAR user. Station data is served by the API and is outside PowerSync.

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
