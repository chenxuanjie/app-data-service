"""XY-CAR account and installer release registration."""

from app_data_service.applications import ApplicationDefinition

definition = ApplicationDefinition(
    id="xy_car_app",
    name="XY-CAR",
    schema="xy_car_app",
    personal_data=False,
    public_file_areas=("releases",),
    capabilities={"public_releases": 1},
)
