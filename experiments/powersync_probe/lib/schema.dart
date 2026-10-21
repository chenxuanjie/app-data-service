import 'package:powersync/powersync.dart';

// The framework supplies the single text id column. Amount is NUMERIC -> text.
const expenseSchema = Schema([
  Table('expenses', [
    Column.text('dataset_id'),
    Column.text('name'),
    Column.text('amount'),
    Column.text('cycle'),
    Column.text('category'),
    Column.text('subcategory'),
    Column.text('icon_id'),
    Column.integer('include_in_living_cost'),
    Column.text('note'),
    Column.integer('sort_order'),
    Column.integer('row_version'),
    Column.text('updated_at'),
    Column.text('deleted_at'),
  ], trackMetadata: true),
  Table('assets', [
    Column.text('dataset_id'),
    Column.text('name'),
    Column.text('price'),
    Column.text('purchase_date'),
    Column.text('category'),
    Column.text('residual_value'),
    Column.text('target_daily_cost'),
    Column.text('icon_name'),
    Column.integer('color_value'),
    Column.integer('include_in_living_cost'),
    Column.text('note'),
    Column.integer('sort_order'),
    Column.integer('row_version'),
    Column.text('updated_at'),
    Column.text('deleted_at'),
  ], trackMetadata: true),
  Table('expense_categories', [
    Column.text('dataset_id'),
    Column.text('name'),
    Column.text('icon_id'),
    Column.integer('sort_order'),
    Column.integer('row_version'),
    Column.text('updated_at'),
    Column.text('deleted_at'),
  ], trackMetadata: true),
  Table.localOnly('business_mutation_heads', [
    Column.integer('base_version'),
    Column.text('mutation_id'),
  ]),
  Table.localOnly('mutation_heads', [
    Column.integer('base_version'),
    Column.text('mutation_id'),
  ]),
]);
