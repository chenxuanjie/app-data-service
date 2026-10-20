import 'package:app_data_powersync_probe/schema.dart';
import 'package:powersync/powersync.dart';

// Compile-only probe. Runtime browser verification requires SDK worker assets.
Future<void> main() async {
  final database = PowerSyncDatabase(
    schema: expenseSchema,
    path: 'app-data-service-compatibility.sqlite',
  );
  await database.initialize();
  await database.close();
}
