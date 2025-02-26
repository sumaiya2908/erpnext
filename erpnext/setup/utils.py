# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import frappe
from frappe import _
from frappe.utils import add_days, flt, get_datetime_str, nowdate
from frappe.utils.data import now_datetime
from frappe.utils.nestedset import get_ancestors_of, get_root_of  # noqa

from erpnext import get_default_company


def before_tests():
	frappe.clear_cache()
	# complete setup if missing
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
	current_year = now_datetime().year
	if not frappe.get_list("Company"):
		setup_complete({
			"currency"          :"USD",
			"full_name"         :"Test User",
			"company_name"      :"Wind Power LLC",
			"timezone"          :"America/New_York",
			"company_abbr"      :"WP",
			"industry"          :"Manufacturing",
			"country"           :"United States",
			"fy_start_date"     :f"{current_year}-01-01",
			"fy_end_date"       :f"{current_year}-12-31",
			"language"          :"english",
			"company_tagline"   :"Testing",
			"email"             :"test@erpnext.com",
			"password"          :"test",
			"chart_of_accounts" : "Standard",
			"domains"           : ["Manufacturing"],
		})

	frappe.db.sql("delete from `tabLeave Allocation`")
	frappe.db.sql("delete from `tabLeave Application`")
	frappe.db.sql("delete from `tabSalary Slip`")
	frappe.db.sql("delete from `tabItem Price`")

	enable_all_roles_and_domains()
	set_defaults_for_tests()

	frappe.db.commit()

@frappe.whitelist()
def get_exchange_rate(from_currency, to_currency, transaction_date=None, args=None):
	if not (from_currency and to_currency):
		# manqala 19/09/2016: Should this be an empty return or should it throw and exception?
		return
	if from_currency == to_currency:
		return 1

	if not transaction_date:
		transaction_date = nowdate()
	currency_settings = frappe.get_doc("Accounts Settings").as_dict()
	allow_stale_rates = currency_settings.get("allow_stale")

	filters = [
		["date", "<=", get_datetime_str(transaction_date)],
		["from_currency", "=", from_currency],
		["to_currency", "=", to_currency]
	]

	if args == "for_buying":
		filters.append(["for_buying", "=", "1"])
	elif args == "for_selling":
		filters.append(["for_selling", "=", "1"])

	if not allow_stale_rates:
		stale_days = currency_settings.get("stale_days")
		checkpoint_date = add_days(transaction_date, -stale_days)
		filters.append(["date", ">", get_datetime_str(checkpoint_date)])

	# cksgb 19/09/2016: get last entry in Currency Exchange with from_currency and to_currency.
	entries = frappe.get_all(
		"Currency Exchange", fields=["exchange_rate"], filters=filters, order_by="date desc",
		limit=1)
	if entries:
		return flt(entries[0].exchange_rate)

	try:
		cache = frappe.cache()
		key = "currency_exchange_rate_{0}:{1}:{2}".format(transaction_date, from_currency, to_currency)
		value = cache.get(key)

		if not value:
			import requests
			settings = frappe.get_cached_doc('Currency Exchange Settings')
			req_params = {
				"transaction_date": transaction_date,
				"from_currency": from_currency,
				"to_currency": to_currency
			}
			params = {}
			for row in settings.req_params:
				params[row.key] = format_ces_api(row.value, req_params)
			response = requests.get(format_ces_api(settings.api_endpoint, req_params), params=params)
			# expire in 6 hours
			response.raise_for_status()
			value = response.json()
			for res_key in settings.result_key:
				value = value[format_ces_api(str(res_key.key), req_params)]
			cache.setex(name=key, time=21600, value=flt(value))
		return flt(value)
	except Exception:
		frappe.log_error(title="Get Exchange Rate")
		frappe.msgprint(_("Unable to find exchange rate for {0} to {1} for key date {2}. Please create a Currency Exchange record manually").format(from_currency, to_currency, transaction_date))
		return 0.0

def format_ces_api(data, param):
	return data.format(
		transaction_date=param.get("transaction_date"),
		to_currency=param.get("to_currency"),
		from_currency=param.get("from_currency")
	)

def enable_all_roles_and_domains():
	""" enable all roles and domain for testing """
	# add all roles to users
	domains = frappe.get_all("Domain")
	if not domains:
		return

	from frappe.desk.page.setup_wizard.setup_wizard import add_all_roles_to
	frappe.get_single('Domain Settings').set_active_domains(\
		[d.name for d in domains])
	add_all_roles_to('Administrator')

def set_defaults_for_tests():
	selling_settings = frappe.get_single("Selling Settings")
	selling_settings.customer_group = get_root_of("Customer Group")
	selling_settings.territory = get_root_of("Territory")
	selling_settings.save()

	frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 0)


def insert_record(records):
	for r in records:
		doc = frappe.new_doc(r.get("doctype"))
		doc.update(r)
		try:
			doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
		except frappe.DuplicateEntryError as e:
			# pass DuplicateEntryError and continue
			if e.args and e.args[0]==doc.doctype and e.args[1]==doc.name:
				# make sure DuplicateEntryError is for the exact same doc and not a related doc
				pass
			else:
				raise

def welcome_email():
	site_name = get_default_company() or "ERPNext"
	title = _("Welcome to {0}").format(site_name)
	return title
