import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:app_data_powersync_probe/connector.dart';
import 'package:app_data_powersync_probe/schema.dart';
import 'package:powersync/powersync.dart';

Future<void> until(Future<bool> Function() predicate, String label) async {
  final deadline = DateTime.now().add(const Duration(seconds: 35));
  while (!await predicate()) {
    if (DateTime.now().isAfter(deadline)) throw StateError('Timeout: $label');
    await Future<void>.delayed(const Duration(milliseconds: 100));
  }
}

void check(bool result, String message) {
  if (!result) throw StateError(message);
}

Future<void> main(List<String> args) async {
  final config =
      jsonDecode(await File(args.single).readAsString())
          as Map<String, dynamic>;
  final directory = config['directory'] as String;
  PowerSyncDatabase database(String name) => PowerSyncDatabase(
    schema: expenseSchema,
    path: '$directory${Platform.pathSeparator}$name.sqlite',
  );
  final aliceDataset =
      (config['datasets']?['alice'] as String?) ?? 'alice-data';
  final bobDataset = (config['datasets']?['bob'] as String?) ?? 'bob-data';
  var a = database('alice-a');
  final b = database('alice-b');
  final bob = database('bob');
  ProbeConnector connector(String user, String dataset) => ProbeConnector(
    Uri.parse(config['api'] as String),
    config['tokens'][user] as String,
    dataset,
  );
  final ca = connector('alice', aliceDataset);
  final cb = connector('alice', aliceDataset);
  final cc = connector('bob', bobDataset);
  final record = <String, dynamic>{
    'id': 'live-existing-id',
    'name': '离线套餐',
    'amount': '59.10',
    'cycle': 'monthly',
    'category': '通信',
    'subcategory': '手机',
  };
  Future<Map<String, dynamic>?> row(PowerSyncDatabase db) =>
      db.getOptional('SELECT * FROM expenses WHERE id=?', [record['id']]);
  Future<void> drain(PowerSyncDatabase db) => until(
    () async => (await db.getUploadQueueStats()).count == 0,
    'upload queue',
  );
  final checks = <String>[];
  try {
    await Future.wait([a.initialize(), b.initialize(), bob.initialize()]);
    await saveExpense(a, aliceDataset, record);
    final clientId = await a.getClientId();
    await a.close();
    a = database('alice-a');
    await a.initialize();
    check(
      await a.getClientId() == clientId,
      'Client identity changed after restart',
    );
    check((await a.getUploadQueueStats()).count == 1, 'Offline queue lost');
    record['name'] = '离线连续编辑';
    await saveExpense(a, aliceDataset, record);
    checks.add('offline_restart_and_chained_edits');

    await Future.wait([b.connect(connector: cb), bob.connect(connector: cc)]);
    await Future.wait([
      b.waitForFirstSync(),
      bob.waitForFirstSync(),
    ]).timeout(const Duration(seconds: 40));
    check(await row(b) == null, 'Unexpected initial sample');
    check(
      (await b.getAll('SELECT id FROM expenses')).isEmpty,
      'Another application leaked into the Zheri stream',
    );
    check(
      (await bob.getAll('SELECT id FROM expenses')).isEmpty,
      'Unexpected data for the other account',
    );
    ca.simulateLostResponse = true;
    await a.connect(connector: ca);
    await drain(a);
    await until(() async => (await row(b))?['row_version'] == 2, 'A to B');
    check((await row(b))?['amount'] == '59.10', 'Decimal precision changed');
    check((await row(b))?['name'] == '离线连续编辑', 'Chained edit was rejected');
    check(ca.replayedResponses == 1, 'Lost response path not exercised');
    checks.add('real_upload_replication_download_and_idempotent_retry');
    check(await row(bob) == null, 'Another account received private expense');
    checks.add('other_account_download_isolation');

    await until(
      () async => (await row(a))?['row_version'] == 2,
      'A confirmation',
    );
    await Future.wait([a.disconnect(), b.disconnect()]);
    record['name'] = '设备甲版本';
    await saveExpense(a, aliceDataset, record);
    record['name'] = '设备乙版本';
    await saveExpense(b, aliceDataset, record);
    await a.connect(connector: ca);
    await drain(a);
    await until(() async => (await row(a))?['row_version'] == 3, 'A version 3');
    await b.connect(connector: cb);
    await drain(b);
    await until(
      () async => cb.outcomes.any((r) => r['status'] == 'conflict'),
      'conflict retained',
    );
    final response = await cb.request(
      '/v1/conflicts?dataset_id=${Uri.encodeQueryComponent(aliceDataset)}',
      get: true,
    );
    final conflicts = response['conflicts'] as List;
    check(conflicts.length == 1, 'Expected one retained conflict');
    final conflict = conflicts.single as Map<String, dynamic>;
    check(conflict['candidate']['name'] == '设备乙版本', 'Candidate lost');
    check(conflict['server_record']['name'] == '设备甲版本', 'Server version lost');
    await cb.request(
      '/v1/conflicts/${conflict['id']}/resolve',
      body: {'choice': 'candidate', 'expected_version': 3},
    );
    await until(() async => (await row(a))?['row_version'] == 4, 'resolved A');
    await until(() async => (await row(b))?['row_version'] == 4, 'resolved B');
    check(
      (await row(a))?['name'] == '设备乙版本',
      'Selected version not replicated',
    );
    checks.add('concurrent_versions_retained_and_choice_replicated');

    await b.disconnect();
    await saveExpense(b, aliceDataset, record, delete: true);
    check((await row(b))?['deleted_at'] != null, 'Offline delete not saved');
    await b.connect(connector: cb);
    await drain(b);
    await until(() async => await row(a) == null, 'delete A');
    await until(() async => await row(b) == null, 'delete B');
    check(await row(bob) == null, 'Other account leaked after updates');
    checks.add('offline_delete_reconnect_and_two_client_convergence');
    if (config['multi_entity'] == true) {
      // Exercise the new publication and streams using real SDK queues and
      // three independent persistent SQLite clients, including a shared raw ID.
      check(
        (await b.getAll('SELECT id FROM assets')).isEmpty,
        'Other-app assets leaked',
      );
      check(
        (await b.getAll('SELECT id FROM expense_categories')).isEmpty,
        'Other-app categories leaked',
      );
      final category = <String, dynamic>{
        'id': 'live-typed-id',
        'name': '新分类',
        'iconId': 'custom',
        'sortOrder': 0,
      };
      final asset = <String, dynamic>{
        'id': 'live-typed-id',
        'name': '离线手机',
        'price': '6899.100001',
        'purchaseDate': '2024-02-29',
        'category': '数码',
        'residualValue': '2180',
        'targetDailyCost': '5',
        'icon': 'smartphone',
        'color': 4294967295,
        'includeInLivingCost': true,
        'note': '保留六位小数',
        'sortOrder': 0,
      };
      Future<Map<String, dynamic>?> businessRow(
        PowerSyncDatabase db,
        String table,
      ) => db.getOptional('SELECT * FROM $table WHERE id=?', ['live-typed-id']);
      await a.disconnect();
      await saveBusiness(a, aliceDataset, 'expense_categories', category);
      await saveBusiness(a, aliceDataset, 'assets', asset);
      await a.close();
      a = database('alice-a');
      await a.initialize();
      check(
        (await a.getUploadQueueStats()).count == 2,
        'Typed offline queues lost on restart',
      );
      await a.connect(connector: ca);
      await drain(a);
      await until(
        () async => (await businessRow(b, 'assets'))?['row_version'] == 1,
        'typed asset download',
      );
      await until(
        () async =>
            (await businessRow(b, 'expense_categories'))?['row_version'] == 1,
        'typed category download',
      );
      check(
        (await businessRow(b, 'assets'))?['price'] == '6899.100001',
        'Asset money rounded',
      );
      check(
        (await businessRow(b, 'assets'))?['color_value'] == 4294967295,
        'ARGB overflow',
      );
      check(
        (await businessRow(b, 'assets'))?['purchase_date'] == '2024-02-29',
        'Calendar day shifted',
      );
      check(
        await businessRow(bob, 'assets') == null &&
            await businessRow(bob, 'expense_categories') == null,
        'Typed account isolation failed',
      );
      checks.add(
        'typed_offline_restart_exact_values_replication_and_isolation',
      );
      await until(
        () async => (await businessRow(a, 'assets'))?['row_version'] == 1,
        'typed A confirmation',
      );
      await Future.wait([a.disconnect(), b.disconnect()]);
      asset['name'] = '物品甲';
      await saveBusiness(a, aliceDataset, 'assets', asset);
      asset['name'] = '物品乙';
      await saveBusiness(b, aliceDataset, 'assets', asset);
      await a.connect(connector: ca);
      await drain(a);
      await until(
        () async => (await businessRow(a, 'assets'))?['row_version'] == 2,
        'typed winner',
      );
      await b.connect(connector: cb);
      await drain(b);
      final typedConflicts = await cb.request(
        '/v2/conflicts?dataset_id=${Uri.encodeQueryComponent(aliceDataset)}',
        get: true,
      );
      final typedConflict = (typedConflicts['conflicts'] as List)
          .cast<Map<String, dynamic>>()
          .singleWhere((r) => r['entity_type'] == 'assets');
      check(
        typedConflict['candidate']['name'] == '物品乙',
        'Typed candidate lost',
      );
      await cb.request(
        '/v2/conflicts/${typedConflict['id']}/resolve',
        body: {
          'entity_type': 'assets',
          'choice': 'candidate',
          'expected_version': 2,
        },
      );
      await until(
        () async => (await businessRow(a, 'assets'))?['row_version'] == 3,
        'typed conflict A resolved',
      );
      await until(
        () async => (await businessRow(b, 'assets'))?['row_version'] == 3,
        'typed conflict B resolved',
      );
      checks.add('typed_concurrent_versions_and_resolution_replicate');
      await b.disconnect();
      await saveBusiness(
        b,
        aliceDataset,
        'expense_categories',
        category,
        delete: true,
      );
      await saveBusiness(b, aliceDataset, 'assets', asset, delete: true);
      await b.connect(connector: cb);
      await drain(b);
      await until(
        () async =>
            await businessRow(a, 'assets') == null &&
            await businessRow(a, 'expense_categories') == null,
        'typed delete A',
      );
      await until(
        () async =>
            await businessRow(b, 'assets') == null &&
            await businessRow(b, 'expense_categories') == null,
        'typed delete B',
      );
      check(
        await businessRow(bob, 'assets') == null,
        'Typed delete leaked data',
      );
      checks.add('typed_delete_reconnect_converges');
    }
    if (config['full_business'] == true) {
      const tables = [
        'payroll_records',
        'asset_allocations',
        'business_preferences',
      ];
      final records = <String, Map<String, dynamic>>{
        'payroll_records': {
          'id': 'live-payroll',
          'name': '2026-10',
          'sortOrder': 0,
          'payload': {
            'monthKey': '2026-10',
            'salary': '10000.123456',
            'personalConsumption': '1000',
            'foodBudget': '100',
            'rentProperty': '2000',
            'parentSupport': '1200',
            'allocations': [],
            'recordedAt': '2026-10-21T12:00:00Z',
            'fixedSavings': '1000',
            'investmentRatio': '0.8',
            'savingsRatio': '0.2',
            'note': '历史月份',
            'updatedAt': null,
          },
        },
        'asset_allocations': {
          'id': 'live-allocation',
          'name': '配置原值',
          'sortOrder': 0,
          'payload': {
            'color': 4294967295,
            'currentAmount': '5000.123456',
            'ruleType': 'target',
            'ruleRatio': '0.333333333333333333',
            'historicalHighPrice': '100',
            'addPositionSteps': [],
          },
        },
        'business_preferences': {
          'id': 'live-preference',
          'name': 'allocation_baseline',
          'sortOrder': 0,
          'payload': '30000.123456',
        },
      };
      Future<Map<String, dynamic>?> docRow(
        PowerSyncDatabase db,
        String entity,
      ) => db.getOptional('SELECT * FROM $entity WHERE id=?', [
        records[entity]!['id'],
      ]);
      await a.disconnect();
      for (final entity in tables)
        await saveBusiness(a, aliceDataset, entity, records[entity]!);
      final sameClient = await a.getClientId();
      await a.close();
      a = database('alice-a');
      await a.initialize();
      check(
        await a.getClientId() == sameClient,
        'Full business client changed on restart',
      );
      await a.connect(connector: ca);
      await drain(a);
      for (final entity in tables) {
        await until(
          () async => (await docRow(b, entity))?['row_version'] == 1,
          '$entity downloaded',
        );
        final payload = jsonDecode(
          (await docRow(b, entity))!['payload'] as String,
        );
        check(jsonEncode(payload) != 'null', 'Document missing');
        check(
          await docRow(bob, entity) == null,
          'Full business leaked to other account',
        );
      }
      checks.add(
        'full_business_offline_restart_and_three_entity_replication_isolation',
      );
      for (final entity in tables) {
        await a.disconnect();
        await b.disconnect();
        final original = records[entity]!;
        final winner = {
          ...original,
          'payload': entity == 'business_preferences'
              ? '40000'
              : {
                  ...original['payload'] as Map,
                  entity == 'payroll_records' ? 'salary' : 'currentAmount':
                      '6000',
                },
        };
        final candidate = {
          ...original,
          'payload': entity == 'business_preferences'
              ? '50000'
              : {
                  ...original['payload'] as Map,
                  entity == 'payroll_records' ? 'salary' : 'currentAmount':
                      '7000',
                },
        };
        await saveBusiness(a, aliceDataset, entity, winner);
        await saveBusiness(b, aliceDataset, entity, candidate);
        await a.connect(connector: ca);
        await drain(a);
        await until(
          () async => (await docRow(a, entity))?['row_version'] == 2,
          '$entity winner',
        );
        await b.connect(connector: cb);
        await drain(b);
        final conflicts = await cb.request(
          '/v2/conflicts?dataset_id=$aliceDataset',
          get: true,
        );
        final conflict = (conflicts['conflicts'] as List)
            .cast<Map>()
            .singleWhere((e) => e['entity_type'] == entity);
        await cb.request(
          '/v2/conflicts/${conflict['id']}/resolve',
          body: {
            'entity_type': entity,
            'choice': 'candidate',
            'expected_version': 2,
          },
        );
        await until(
          () async =>
              (await docRow(a, entity))?['row_version'] == 3 &&
              (await docRow(b, entity))?['row_version'] == 3,
          '$entity conflict resolved',
        );
      }
      checks.add(
        'full_business_payroll_allocation_preference_conflicts_resolve_and_replicate',
      );
      final promoted = await ca.request(
        '/v2/datasets/$aliceDataset/promote',
        body: {},
      );
      check(
        promoted['dataset_id'] == aliceDataset &&
            promoted['dataset_kind'] == 'personal',
        'Promotion changed identity',
      );
      await b.disconnect();
      for (final entity in tables)
        await saveBusiness(
          b,
          aliceDataset,
          entity,
          records[entity]!,
          delete: true,
        );
      await b.connect(connector: cb);
      await drain(b);
      for (final entity in tables) {
        await until(
          () async =>
              await docRow(a, entity) == null &&
              await docRow(b, entity) == null,
          '$entity deleted after promotion',
        );
      }
      checks.add(
        'full_business_personal_promotion_preserves_queue_and_delete_converges',
      );
    }
    stdout.writeln(
      jsonEncode({'passed': checks, 'platform': Platform.operatingSystem}),
    );
  } finally {
    await Future.wait([a.close(), b.close(), bob.close()]);
    ca.close();
    cb.close();
    cc.close();
  }
}
