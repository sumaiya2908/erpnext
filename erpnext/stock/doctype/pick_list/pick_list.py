# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
from collections import OrderedDict, defaultdict
from itertools import groupby
from operator import itemgetter

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import map_child_doc
from frappe.utils import cint, floor, flt, today

from erpnext.selling.doctype.sales_order.sales_order import (
	make_delivery_note as create_delivery_note_from_sales_order,
)
from erpnext.stock.get_item_details import get_conversion_factor

# TODO: Prioritize SO or WO group warehouse

class PickList(Document):
	def validate(self):
		self.validate_for_qty()

	def before_save(self):
		self.set_item_locations()

		# set percentage picked in SO
		for location in self.get('locations'):
			if location.sales_order and frappe.db.get_value("Sales Order",location.sales_order,"per_picked") == 100:
				frappe.throw("Row " + str(location.idx) + " has been picked already!")

	def before_submit(self):
		for item in self.locations:
			# if the user has not entered any picked qty, set it to stock_qty, before submit
			if item.picked_qty == 0:
				item.picked_qty = item.stock_qty

			if item.sales_order_item:
				# update the picked_qty in SO Item
				self.update_so(item.sales_order_item,item.picked_qty,item.item_code)

			if not frappe.get_cached_value('Item', item.item_code, 'has_serial_no'):
				continue
			if not item.serial_no:
				frappe.throw(_("Row #{0}: {1} does not have any available serial numbers in {2}").format(
					frappe.bold(item.idx), frappe.bold(item.item_code), frappe.bold(item.warehouse)),
					title=_("Serial Nos Required"))
			if len(item.serial_no.split('\n')) == item.picked_qty:
				continue
			frappe.throw(_('For item {0} at row {1}, count of serial numbers does not match with the picked quantity')
				.format(frappe.bold(item.item_code), frappe.bold(item.idx)), title=_("Quantity Mismatch"))

	def before_cancel(self):
		#update picked_qty in SO Item on cancel of PL
		for location in self.get('locations'):
			if location.sales_order_item:
				self.update_so(location.sales_order_item,0,location.item_code)

	def update_so(self,so_item,picked_qty,item_code):
		so_doc = frappe.get_doc("Sales Order",frappe.db.get_value("Sales Order Item",so_item,"parent"))
		already_picked,actual_qty = frappe.db.get_value("Sales Order Item",so_item,["picked_qty","qty"])

		if self.docstatus == 1:
			if (((already_picked + picked_qty)/ actual_qty)*100) > (100 + flt(frappe.db.get_single_value('Stock Settings', 'over_delivery_receipt_allowance'))):
				frappe.throw('You are picking more than required quantity for ' + item_code + '. Check if there is any other pick list created for '+so_doc.name)

		frappe.db.set_value("Sales Order Item",so_item,"picked_qty",already_picked+picked_qty)

		total_picked_qty = 0
		total_so_qty = 0
		for item in so_doc.get('items'):
			total_picked_qty += flt(item.picked_qty)
			total_so_qty += flt(item.stock_qty)
		total_picked_qty=total_picked_qty + picked_qty
		per_picked = total_picked_qty/total_so_qty * 100

		so_doc.db_set("per_picked", flt(per_picked) ,update_modified=False)

	@frappe.whitelist()
	def set_item_locations(self, save=False):
		self.validate_for_qty()
		items = self.aggregate_item_qty()
		self.item_location_map = frappe._dict()

		from_warehouses = None
		if self.parent_warehouse:
			from_warehouses = frappe.db.get_descendants('Warehouse', self.parent_warehouse)

		# Create replica before resetting, to handle empty table on update after submit.
		locations_replica  = self.get('locations')

		# reset
		self.delete_key('locations')
		for item_doc in items:
			item_code = item_doc.item_code

			self.item_location_map.setdefault(item_code,
				get_available_item_locations(item_code, from_warehouses, self.item_count_map.get(item_code), self.company))

			locations = get_items_with_location_and_quantity(item_doc, self.item_location_map, self.docstatus)

			item_doc.idx = None
			item_doc.name = None

			for row in locations:
				location = item_doc.as_dict()
				location.update(row)
				self.append('locations', location)

		# If table is empty on update after submit, set stock_qty, picked_qty to 0 so that indicator is red
		# and give feedback to the user. This is to avoid empty Pick Lists.
		if not self.get('locations') and self.docstatus == 1:
			for location in locations_replica:
				location.stock_qty = 0
				location.picked_qty = 0
				self.append('locations', location)
			frappe.msgprint(_("Please Restock Items and Update the Pick List to continue. To discontinue, cancel the Pick List."),
				 title=_("Out of Stock"), indicator="red")

		if save:
			self.save()

	def aggregate_item_qty(self):
		locations = self.get('locations')
		self.item_count_map = {}
		# aggregate qty for same item
		item_map = OrderedDict()
		for item in locations:
			if not item.item_code:
				frappe.throw("Row #{0}: Item Code is Mandatory".format(item.idx))
			item_code = item.item_code
			reference = item.sales_order_item or item.material_request_item
			key = (item_code, item.uom, reference)

			item.idx = None
			item.name = None

			if item_map.get(key):
				item_map[key].qty += item.qty
				item_map[key].stock_qty += item.stock_qty
			else:
				item_map[key] = item

			# maintain count of each item (useful to limit get query)
			self.item_count_map.setdefault(item_code, 0)
			self.item_count_map[item_code] += item.stock_qty

		return item_map.values()

	def validate_for_qty(self):
		if self.purpose == "Material Transfer for Manufacture" \
				and (self.for_qty is None or self.for_qty == 0):
			frappe.throw(_("Qty of Finished Goods Item should be greater than 0."))

	def before_print(self, settings=None):
		if self.get("group_same_items"):
			self.group_similar_items()

	def group_similar_items(self):
		group_item_qty = defaultdict(float)
		group_picked_qty = defaultdict(float)

		for item in self.locations:
			group_item_qty[(item.item_code, item.warehouse)] +=  item.qty
			group_picked_qty[(item.item_code, item.warehouse)] += item.picked_qty

		duplicate_list = []
		for item in self.locations:
			if (item.item_code, item.warehouse) in group_item_qty:
				item.qty = group_item_qty[(item.item_code, item.warehouse)]
				item.picked_qty = group_picked_qty[(item.item_code, item.warehouse)]
				item.stock_qty = group_item_qty[(item.item_code, item.warehouse)]
				del group_item_qty[(item.item_code, item.warehouse)]
			else:
				duplicate_list.append(item)

		for item in duplicate_list:
			self.remove(item)

		for idx, item in enumerate(self.locations, start=1):
			item.idx = idx


def validate_item_locations(pick_list):
	if not pick_list.locations:
		frappe.throw(_("Add items in the Item Locations table"))

def get_items_with_location_and_quantity(item_doc, item_location_map, docstatus):
	available_locations = item_location_map.get(item_doc.item_code)
	locations = []

	# if stock qty is zero on submitted entry, show positive remaining qty to recalculate in case of restock.
	remaining_stock_qty = item_doc.qty if (docstatus == 1 and item_doc.stock_qty == 0) else item_doc.stock_qty

	while remaining_stock_qty > 0 and available_locations:
		item_location = available_locations.pop(0)
		item_location = frappe._dict(item_location)

		stock_qty = remaining_stock_qty if item_location.qty >= remaining_stock_qty else item_location.qty
		qty = stock_qty / (item_doc.conversion_factor or 1)

		uom_must_be_whole_number = frappe.db.get_value('UOM', item_doc.uom, 'must_be_whole_number')
		if uom_must_be_whole_number:
			qty = floor(qty)
			stock_qty = qty * item_doc.conversion_factor
			if not stock_qty: break

		serial_nos = None
		if item_location.serial_no:
			serial_nos = '\n'.join(item_location.serial_no[0: cint(stock_qty)])

		locations.append(frappe._dict({
			'qty': qty,
			'stock_qty': stock_qty,
			'warehouse': item_location.warehouse,
			'serial_no': serial_nos,
			'batch_no': item_location.batch_no
		}))

		remaining_stock_qty -= stock_qty

		qty_diff = item_location.qty - stock_qty
		# if extra quantity is available push current warehouse to available locations
		if qty_diff > 0:
			item_location.qty = qty_diff
			if item_location.serial_no:
				# set remaining serial numbers
				item_location.serial_no = item_location.serial_no[-int(qty_diff):]
			available_locations = [item_location] + available_locations

	# update available locations for the item
	item_location_map[item_doc.item_code] = available_locations
	return locations

def get_available_item_locations(item_code, from_warehouses, required_qty, company, ignore_validation=False):
	locations = []
	has_serial_no  = frappe.get_cached_value('Item', item_code, 'has_serial_no')
	has_batch_no = frappe.get_cached_value('Item', item_code, 'has_batch_no')

	if has_batch_no and has_serial_no:
		locations = get_available_item_locations_for_serial_and_batched_item(item_code, from_warehouses, required_qty, company)
	elif has_serial_no:
		locations = get_available_item_locations_for_serialized_item(item_code, from_warehouses, required_qty, company)
	elif has_batch_no:
		locations = get_available_item_locations_for_batched_item(item_code, from_warehouses, required_qty, company)
	else:
		locations = get_available_item_locations_for_other_item(item_code, from_warehouses, required_qty, company)

	total_qty_available = sum(location.get('qty') for location in locations)

	remaining_qty = required_qty - total_qty_available

	if remaining_qty > 0 and not ignore_validation:
		frappe.msgprint(_('{0} units of Item {1} is not available.')
			.format(remaining_qty, frappe.get_desk_link('Item', item_code)),
			title=_("Insufficient Stock"))

	return locations


def get_available_item_locations_for_serialized_item(item_code, from_warehouses, required_qty, company):
	filters = frappe._dict({
		'item_code': item_code,
		'company': company,
		'warehouse': ['!=', '']
	})

	if from_warehouses:
		filters.warehouse = ['in', from_warehouses]

	serial_nos = frappe.get_all('Serial No',
		fields=['name', 'warehouse'],
		filters=filters,
		limit=required_qty,
		order_by='purchase_date',
		as_list=1)

	warehouse_serial_nos_map = frappe._dict()
	for serial_no, warehouse in serial_nos:
		warehouse_serial_nos_map.setdefault(warehouse, []).append(serial_no)

	locations = []
	for warehouse, serial_nos in warehouse_serial_nos_map.items():
		locations.append({
			'qty': len(serial_nos),
			'warehouse': warehouse,
			'serial_no': serial_nos
		})

	return locations

def get_available_item_locations_for_batched_item(item_code, from_warehouses, required_qty, company):
	warehouse_condition = 'and warehouse in %(warehouses)s' if from_warehouses else ''
	batch_locations = frappe.db.sql("""
		SELECT
			sle.`warehouse`,
			sle.`batch_no`,
			SUM(sle.`actual_qty`) AS `qty`
		FROM
			`tabStock Ledger Entry` sle, `tabBatch` batch
		WHERE
			sle.batch_no = batch.name
			and sle.`item_code`=%(item_code)s
			and sle.`company` = %(company)s
			and batch.disabled = 0
			and sle.is_cancelled=0
			and IFNULL(batch.`expiry_date`, '2200-01-01') > %(today)s
			{warehouse_condition}
		GROUP BY
			sle.`warehouse`,
			sle.`batch_no`,
			sle.`item_code`
		HAVING `qty` > 0
		ORDER BY IFNULL(batch.`expiry_date`, '2200-01-01'), batch.`creation`
	""".format(warehouse_condition=warehouse_condition), { #nosec
		'item_code': item_code,
		'company': company,
		'today': today(),
		'warehouses': from_warehouses
	}, as_dict=1)

	return batch_locations

def get_available_item_locations_for_serial_and_batched_item(item_code, from_warehouses, required_qty, company):
	# Get batch nos by FIFO
	locations = get_available_item_locations_for_batched_item(item_code, from_warehouses, required_qty, company)

	filters = frappe._dict({
		'item_code': item_code,
		'company': company,
		'warehouse': ['!=', ''],
		'batch_no': ''
	})

	# Get Serial Nos by FIFO for Batch No
	for location in locations:
		filters.batch_no = location.batch_no
		filters.warehouse = location.warehouse
		location.qty = required_qty if location.qty > required_qty else location.qty # if extra qty in batch

		serial_nos = frappe.get_list('Serial No',
			fields=['name'],
			filters=filters,
			limit=location.qty,
			order_by='purchase_date')

		serial_nos = [sn.name for sn in serial_nos]
		location.serial_no = serial_nos

	return locations

def get_available_item_locations_for_other_item(item_code, from_warehouses, required_qty, company):
	# gets all items available in different warehouses
	warehouses = [x.get('name') for x in frappe.get_list("Warehouse", {'company': company}, "name")]

	filters = frappe._dict({
		'item_code': item_code,
		'warehouse': ['in', warehouses],
		'actual_qty': ['>', 0]
	})

	if from_warehouses:
		filters.warehouse = ['in', from_warehouses]

	item_locations = frappe.get_all('Bin',
		fields=['warehouse', 'actual_qty as qty'],
		filters=filters,
		limit=required_qty,
		order_by='creation')

	return item_locations


@frappe.whitelist()
def create_delivery_note(source_name, target_doc=None):
	pick_list = frappe.get_doc('Pick List', source_name)
	validate_item_locations(pick_list)
	sales_dict = dict()
	sales_orders = []
	delivery_note = None
	for location in pick_list.locations:
		if location.sales_order:
			sales_orders.append([frappe.db.get_value("Sales Order",location.sales_order,'customer'),location.sales_order])
	# Group sales orders by customer
	for key,keydata in groupby(sales_orders,key=itemgetter(0)):
		sales_dict[key] = set([d[1] for d in keydata])

	if sales_dict:
		delivery_note = create_dn_with_so(sales_dict,pick_list)

	is_item_wo_so = 0
	for location in pick_list.locations :
		if not location.sales_order:
			is_item_wo_so = 1
			break
	if is_item_wo_so == 1:
		# Create a DN for items without sales orders as well
		delivery_note = create_dn_wo_so(pick_list)

	frappe.msgprint(_('Delivery Note(s) created for the Pick List'))
	return delivery_note

def create_dn_wo_so(pick_list):
		delivery_note = frappe.new_doc("Delivery Note")

		item_table_mapper_without_so = {
			'doctype': 'Delivery Note Item',
			'field_map': {
				'rate': 'rate',
				'name': 'name',
				'parent': '',
			}
		}
		map_pl_locations(pick_list,item_table_mapper_without_so,delivery_note)
		delivery_note.insert(ignore_mandatory = True)

		return delivery_note


def create_dn_with_so(sales_dict,pick_list):
	delivery_note = None

	for customer in sales_dict:
		for so in sales_dict[customer]:
			delivery_note = None
			delivery_note = create_delivery_note_from_sales_order(so,
				delivery_note, skip_item_mapping=True)

			item_table_mapper = {
				'doctype': 'Delivery Note Item',
				'field_map': {
					'rate': 'rate',
					'name': 'so_detail',
					'parent': 'against_sales_order',
				},
				'condition': lambda doc: abs(doc.delivered_qty) < abs(doc.qty) and doc.delivered_by_supplier!=1
			}
			break
		if delivery_note:
			# map all items of all sales orders of that customer
			for so in sales_dict[customer]:
				map_pl_locations(pick_list,item_table_mapper,delivery_note,so)
			delivery_note.insert(ignore_mandatory = True)

	return delivery_note

def map_pl_locations(pick_list,item_mapper,delivery_note,sales_order = None):

	for location in pick_list.locations:
		if location.sales_order == sales_order:
			if location.sales_order_item:
				sales_order_item = frappe.get_cached_doc('Sales Order Item', {'name':location.sales_order_item})
			else:
				sales_order_item = None

			source_doc, table_mapper = [sales_order_item, item_mapper] if sales_order_item \
				else [location, item_mapper]

			dn_item = map_child_doc(source_doc, delivery_note, table_mapper)

			if dn_item:
				dn_item.warehouse = location.warehouse
				dn_item.qty = flt(location.picked_qty) / (flt(location.conversion_factor) or 1)
				dn_item.batch_no = location.batch_no
				dn_item.serial_no = location.serial_no

				update_delivery_note_item(source_doc, dn_item, delivery_note)
	set_delivery_note_missing_values(delivery_note)

	delivery_note.pick_list = pick_list.name
	delivery_note.company = pick_list.company
	delivery_note.customer = frappe.get_value("Sales Order",sales_order,"customer")


@frappe.whitelist()
def create_stock_entry(pick_list):
	pick_list = frappe.get_doc(json.loads(pick_list))
	validate_item_locations(pick_list)

	if stock_entry_exists(pick_list.get('name')):
		return frappe.msgprint(_('Stock Entry has been already created against this Pick List'))

	stock_entry = frappe.new_doc('Stock Entry')
	stock_entry.pick_list = pick_list.get('name')
	stock_entry.purpose = pick_list.get('purpose')
	stock_entry.set_stock_entry_type()

	if pick_list.get('work_order'):
		stock_entry = update_stock_entry_based_on_work_order(pick_list, stock_entry)
	elif pick_list.get('material_request'):
		stock_entry = update_stock_entry_based_on_material_request(pick_list, stock_entry)
	else:
		stock_entry = update_stock_entry_items_with_no_reference(pick_list, stock_entry)

	stock_entry.set_actual_qty()
	stock_entry.calculate_rate_and_amount()

	return stock_entry.as_dict()

@frappe.whitelist()
def get_pending_work_orders(doctype, txt, searchfield, start, page_length, filters, as_dict):
	return frappe.db.sql("""
		SELECT
			`name`, `company`, `planned_start_date`
		FROM
			`tabWork Order`
		WHERE
			`status` not in ('Completed', 'Stopped')
			AND `qty` > `material_transferred_for_manufacturing`
			AND `docstatus` = 1
			AND `company` = %(company)s
			AND `name` like %(txt)s
		ORDER BY
			if(locate(%(_txt)s, name), locate(%(_txt)s, name), 99999), name
		LIMIT
			%(start)s, %(page_length)s""",
		{
			'txt': "%%%s%%" % txt,
			'_txt': txt.replace('%', ''),
			'start': start,
			'page_length': frappe.utils.cint(page_length),
			'company': filters.get('company')
		}, as_dict=as_dict)

@frappe.whitelist()
def target_document_exists(pick_list_name, purpose):
	if purpose == 'Delivery':
		return frappe.db.exists('Delivery Note', {
			'pick_list': pick_list_name
		})

	return stock_entry_exists(pick_list_name)

@frappe.whitelist()
def get_item_details(item_code, uom=None):
	details = frappe.db.get_value('Item', item_code, ['stock_uom', 'name'], as_dict=1)
	details.uom = uom or details.stock_uom
	if uom:
		details.update(get_conversion_factor(item_code, uom))

	return details


def update_delivery_note_item(source, target, delivery_note):
	cost_center = frappe.db.get_value('Project', delivery_note.project, 'cost_center')
	if not cost_center:
		cost_center = get_cost_center(source.item_code, 'Item', delivery_note.company)

	if not cost_center:
		cost_center = get_cost_center(source.item_group, 'Item Group', delivery_note.company)

	target.cost_center = cost_center

def get_cost_center(for_item, from_doctype, company):
	'''Returns Cost Center for Item or Item Group'''
	return frappe.db.get_value('Item Default',
		fieldname=['buying_cost_center'],
		filters={
			'parent': for_item,
			'parenttype': from_doctype,
			'company': company
		})

def set_delivery_note_missing_values(target):
	target.run_method('set_missing_values')
	target.run_method('set_po_nos')
	target.run_method('calculate_taxes_and_totals')

def stock_entry_exists(pick_list_name):
	return frappe.db.exists('Stock Entry', {
		'pick_list': pick_list_name
	})

def update_stock_entry_based_on_work_order(pick_list, stock_entry):
	work_order = frappe.get_doc("Work Order", pick_list.get('work_order'))

	stock_entry.work_order = work_order.name
	stock_entry.company = work_order.company
	stock_entry.from_bom = 1
	stock_entry.bom_no = work_order.bom_no
	stock_entry.use_multi_level_bom = work_order.use_multi_level_bom
	stock_entry.fg_completed_qty = pick_list.for_qty
	if work_order.bom_no:
		stock_entry.inspection_required = frappe.db.get_value('BOM',
			work_order.bom_no, 'inspection_required')

	is_wip_warehouse_group = frappe.db.get_value('Warehouse', work_order.wip_warehouse, 'is_group')
	if not (is_wip_warehouse_group and work_order.skip_transfer):
		wip_warehouse = work_order.wip_warehouse
	else:
		wip_warehouse = None
	stock_entry.to_warehouse = wip_warehouse

	stock_entry.project = work_order.project

	for location in pick_list.locations:
		item = frappe._dict()
		update_common_item_properties(item, location)
		item.t_warehouse = wip_warehouse

		stock_entry.append('items', item)

	return stock_entry

def update_stock_entry_based_on_material_request(pick_list, stock_entry):
	for location in pick_list.locations:
		target_warehouse = None
		if location.material_request_item:
			target_warehouse = frappe.get_value('Material Request Item',
				location.material_request_item, 'warehouse')
		item = frappe._dict()
		update_common_item_properties(item, location)
		item.t_warehouse = target_warehouse
		stock_entry.append('items', item)

	return stock_entry

def update_stock_entry_items_with_no_reference(pick_list, stock_entry):
	for location in pick_list.locations:
		item = frappe._dict()
		update_common_item_properties(item, location)

		stock_entry.append('items', item)

	return stock_entry

def update_common_item_properties(item, location):
	item.item_code = location.item_code
	item.s_warehouse = location.warehouse
	item.qty = location.picked_qty * location.conversion_factor
	item.transfer_qty = location.picked_qty
	item.uom = location.uom
	item.conversion_factor = location.conversion_factor
	item.stock_uom = location.stock_uom
	item.material_request = location.material_request
	item.serial_no = location.serial_no
	item.batch_no = location.batch_no
	item.material_request_item = location.material_request_item