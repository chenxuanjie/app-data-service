import 'dart:async';
import 'dart:io';

import 'package:app_data_powersync_probe/schema.dart';
import 'package:drift/drift.dart' as drift;
import 'package:drift_sqlite_async/drift_sqlite_async.dart';
import 'package:powersync/powersync.dart';
import 'package:test/test.dart';

// No generated tables or createAll(): PowerSync owns the synchronized views.
class ProbeDriftDatabase extends drift.GeneratedDatabase {
  ProbeDriftDatabase(PowerSyncDatabase raw)
    : super.connect(SqliteAsyncDriftConnection(raw));

  @override
  int get schemaVersion => 1;

  @override
  Iterable<drift.TableInfo<drift.Table, Object?>> get allTables => const [];

  @override
  drift.MigrationStrategy get migration => drift.MigrationStrategy(
    onCreate: (_) async {},
    onUpgrade: (_, _, _) async {},
  );
}

void main() {
  late Directory directory;
  late String path;
  late PowerSyncDatabase database;

  Future<void> open() async {
    database = PowerSyncDatabase(schema: expenseSchema, path: path);
    await database.initialize();
  }

  Future<void> insert() => database.execute(
    'INSERT INTO expenses(id,dataset_id,name,amount,cycle,category,subcategory,'
    'include_in_living_cost,note,sort_order,row_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
    [
      'expense-existing-123',
      'dev-zheri',
      '手机套餐',
      '59.10',
      'monthly',
      '通信网络',
      '手机套餐',
      1,
      '',
      0,
      0,
    ],
  );

  setUp(() async {
    directory = await Directory.systemTemp.createTemp('app-data-probe-');
    path = '${directory.path}${Platform.pathSeparator}offline.sqlite';
    await open();
  });

  tearDown(() async {
    if (!database.closed) await database.close();
    // Validate the exact generated temporary directory before recursive cleanup.
    final root = await Directory.systemTemp.resolveSymbolicLinks();
    final resolved = await directory.resolveSymbolicLinks();
    if (!resolved.startsWith('$root${Platform.pathSeparator}app-data-probe-')) {
      throw StateError('Refusing to delete an unexpected directory');
    }
    await directory.delete(recursive: true);
  });

  test(
    'offline save and upload queue survive close/reopen without login',
    () async {
      expect(database.currentStatus.connected, isFalse);
      await insert();
      final clientId = await database.getClientId();
      expect((await database.getUploadQueueStats()).count, 1);
      await database.close();
      await open();
      final row = await database.get('SELECT * FROM expenses');
      expect(row['id'], 'expense-existing-123');
      expect(row['amount'], '59.10');
      expect(row['name'], '手机套餐');
      expect(await database.getClientId(), clientId);
      expect((await database.getUploadQueueStats()).count, 1);
      final transaction = await database.getNextCrudTransaction();
      expect(transaction, isNotNull);
      expect(transaction!.crud.single.op, UpdateType.put);
      expect(transaction.crud.single.opData!['amount'], '59.10');
      // Do not acknowledge: nothing has been uploaded to a server.
    },
  );

  test('offline edit and delete remain queued after reopening', () async {
    await insert();
    await database.execute('UPDATE expenses SET name=? WHERE id=?', [
      '新名称',
      'expense-existing-123',
    ]);
    expect((await database.get('SELECT name FROM expenses'))['name'], '新名称');
    await database.execute('DELETE FROM expenses WHERE id=?', [
      'expense-existing-123',
    ]);
    await database.close();
    await open();
    expect(await database.getAll('SELECT id FROM expenses'), isEmpty);
    expect((await database.getUploadQueueStats()).count, 3);
  });

  test(
    'Drift can query the SDK database without creating duplicate tables',
    () async {
      await insert();
      final orm = ProbeDriftDatabase(database);
      try {
        final row = await orm
            .customSelect('SELECT name,amount FROM expenses')
            .getSingle();
        expect(row.read<String>('name'), '手机套餐');
        expect(row.read<String>('amount'), '59.10');
        final entity = await database.get(
          "SELECT type FROM sqlite_master WHERE name='expenses'",
        );
        expect(entity['type'], 'view');
      } finally {
        await orm.close();
      }
      expect(database.closed, isFalse);
      expect((await database.getUploadQueueStats()).count, 1);
    },
  );

  test('local changes update a watched query while disconnected', () async {
    final initial = Completer<void>();
    final changed = Completer<void>();
    final counts = <int>[];
    final subscription = database.watch('SELECT id FROM expenses').listen((
      rows,
    ) {
      counts.add(rows.length);
      if (rows.isEmpty && !initial.isCompleted) initial.complete();
      if (rows.isNotEmpty && !changed.isCompleted) changed.complete();
    });
    try {
      await initial.future.timeout(const Duration(seconds: 10));
      await insert();
      await changed.future.timeout(const Duration(seconds: 10));
      expect(counts.first, 0);
      expect(counts.last, 1);
      expect(database.currentStatus.connected, isFalse);
    } finally {
      await subscription.cancel();
    }
  });
}
