import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:powersync/powersync.dart';
import 'package:uuid/uuid.dart';

/// Probe only: the app token is minted by the local test runner, not DSM login.
class ProbeConnector extends PowerSyncBackendConnector {
  ProbeConnector(this.api, this.token, this.datasetId);

  final Uri api;
  final String token;
  final String datasetId;
  final http.Client _http = http.Client();
  final List<Map<String, dynamic>> outcomes = [];
  bool simulateLostResponse = false;
  int replayedResponses = 0;

  void close() => _http.close();

  Future<Map<String, dynamic>> request(
    String path, {
    Map<String, dynamic>? body,
    bool get = false,
  }) async {
    final abort = Completer<void>();
    final timer = Timer(const Duration(seconds: 15), abort.complete);
    final request = http.AbortableRequest(
      get ? 'GET' : 'POST',
      api.resolve(path),
      abortTrigger: abort.future,
    );
    request.headers.addAll({
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    });
    if (body != null) request.body = jsonEncode(body);
    try {
      final response = await http.Response.fromStream(
        await _http.send(request),
      ).timeout(const Duration(seconds: 15));
      if (response.statusCode != 200) {
        throw StateError('API $path returned ${response.statusCode}');
      }
      return jsonDecode(response.body) as Map<String, dynamic>;
    } finally {
      timer.cancel();
      if (!abort.isCompleted) abort.complete();
    }
  }

  @override
  Future<PowerSyncCredentials?> fetchCredentials() async =>
      PowerSyncCredentials.fromJson(await request('/v1/sync/credentials'));

  @override
  Future<void> uploadData(PowerSyncDatabase database) async {
    final transaction = await database.getNextCrudTransaction();
    if (transaction == null) return;
    final mutations = <Map<String, dynamic>>[];
    for (final entry in transaction.crud) {
      if (entry.table != 'expenses' || entry.metadata == null) {
        throw StateError('Unmapped queued mutation; leaving it pending');
      }
      final metadata = jsonDecode(entry.metadata!) as Map<String, dynamic>;
      if (metadata['record_id'] != entry.id) {
        throw StateError('Queued mutation identity mismatch');
      }
      mutations.add(metadata);
    }
    final result = await request(
      '/v1/sync/upload',
      body: {
        'dataset_id': datasetId,
        'client_id': await database.getClientId(),
        'mutations': mutations,
      },
    );
    if (simulateLostResponse) {
      simulateLostResponse = false;
      replayedResponses++;
      // The server committed; deliberately don't acknowledge the local queue.
      throw StateError('Simulated loss of committed upload response');
    }
    final results = (result['results'] as List).cast<Map<String, dynamic>>();
    if (results.length != mutations.length ||
        results.indexed.any(
          (pair) =>
              pair.$2['mutation_id'] != mutations[pair.$1]['mutation_id'] ||
              !['applied', 'conflict'].contains(pair.$2['status']),
        )) {
      throw StateError('Invalid upload acknowledgement');
    }
    // Conflicts have been durably retained by the API, so they don't block
    // unrelated uploads. Network/validation/auth failures never clear the queue.
    outcomes.addAll(results);
    await transaction.complete();
  }
}

/// Store a full intended version atomically with its local queue metadata.
Future<void> saveExpense(
  PowerSyncDatabase database,
  String datasetId,
  Map<String, dynamic> record, {
  bool delete = false,
}) async {
  final id = record['id'] as String;
  await database.writeTransaction((tx) async {
    final existing = await tx.getOptional('SELECT * FROM expenses WHERE id=?', [
      id,
    ]);
    final base = (existing?['row_version'] as int?) ?? 0;
    final head = await tx.getOptional(
      'SELECT * FROM mutation_heads WHERE id=?',
      [id],
    );
    final mutationId = const Uuid().v4();
    final metadata = jsonEncode({
      'mutation_id': mutationId,
      'record_id': id,
      'base_version': base,
      'predecessor': (head?['base_version']) == base
          ? (head?['mutation_id'])
          : null,
      'kind': delete ? 'delete' : 'put',
      'record': delete ? null : record,
    });
    final deletedAt = delete ? DateTime.now().toUtc().toIso8601String() : null;
    await tx.execute(
      'INSERT OR REPLACE INTO expenses('
      'id,dataset_id,name,amount,cycle,category,subcategory,icon_id,'
      'include_in_living_cost,note,sort_order,row_version,deleted_at,_metadata) '
      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
      [
        id,
        datasetId,
        record['name'],
        record['amount'],
        record['cycle'],
        record['category'],
        record['subcategory'],
        record['iconId'],
        record['includeInLivingCost'] == false ? 0 : 1,
        record['note'] ?? '',
        record['sortOrder'] ?? 0,
        base,
        deletedAt,
        metadata,
      ],
    );
    await tx.execute(
      'INSERT OR REPLACE INTO mutation_heads(id,base_version,mutation_id) VALUES(?,?,?)',
      [id, base, mutationId],
    );
  });
}
