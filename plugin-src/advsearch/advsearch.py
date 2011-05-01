"""
advsearch.py - Advanced Search Plugin for Trac

This module defines a Trac extension point for the advanced search backend.

See TracAdvancedSearchBackend for more details.
"""

from operator import itemgetter
import pkg_resources
import re
import simplejson

from trac.perm import IPermissionRequestor
from trac.web.chrome import INavigationContributor
from trac.web.chrome import ITemplateProvider
from trac.web.main import IRequestHandler
from trac.wiki.api import IWikiSyntaxProvider

from trac.core import Component
from trac.core import ExtensionPoint
from trac.core import implements
from trac.core import Interface 
from trac.util import escape
from trac.util.html import html
from trac.util.presentation import Paginator
from trac.util.translation import _
from trac.web.chrome import add_stylesheet, add_warning, add_link, add_script


class IAdvSearchBackend(Interface):
	"""Interface to provides a search service."""

	def get_name():
		"""Return the name of this backend."""

	def upsert_document(doc):
		"""
		Accepts a dictionary doc which contains all the data about an updated
		document (wiki page, ticket, etc) to be inserted or updated in the 
		backend index. The keys of the dict should match the field names in
		the database.
		"""


	def query_backend(criteria):
		"""
		Given a dictionary of criteria, perform a query in the search backend
		and return a list of dicts with the results. Backends should ignore any
		criteria it does not know how to deal with.

		Returns a tuple of (total result count, list of results).  Each results
		is a dict with keys: title, score, source, summary, date, author. 
		When multiple providers return results for a source score is used to 
		order the results. 

		Example:
		criteria = {
			'q': 'trac help',
			'author: ['admin', 'joe'],
			'source': ['wiki'],
			'date_start': '2011-04-01',
			'date_end': '2011-04-30',
		}

		return (
			200, 
			[
				{
					'title': 'TracHelp', 
					'score': 0.876, 
					'source': 'wiki', 
					'summary': '==Trac Help== ....'
					'date': '2011-02-34 23:34',
					'author': 'admin',
				},
				...
			]
		)
		"""


class AdvancedSearchPlugin(Component):
	implements(
		INavigationContributor, 
		IPermissionRequestor,
		IRequestHandler,
		IWikiSyntaxProvider,
		ITemplateProvider,
	)
	
	providers = ExtensionPoint(IAdvSearchBackend)

	# TODO: take from source sources 
	SOURCE_FILTERS = ('wiki', 'ticket')
	DEFAULT_PER_PAGE = 10

	# INavigationContributor methods
	def get_active_navigation_item(self, req):
		return 'advsearch'

	def get_navigation_items(self, req):
		if 'SEARCH_VIEW' in req.perm:
			yield ('mainnav', 
				'advsearch', 
				html.A(_('Advanced Search'), href=self.env.href.advsearch())
			)

	# IPermissionRequestor methods
	def get_permission_actions(self):
		return ['SEARCH_VIEW']

	# IRequestHandler methods
	def match_request(self, req):
		# TODO: add /search if search module is disabled
		return re.match(r'/advsearch?', req.path_info) is not None

	def process_request(self, req):
		"""
		Implements IRequestHandler.process_request

		Build a dict of search criteria from the user and request results from 
		the active AdvancedSearchBackend.
		"""
		req.perm.assert_permission('SEARCH_VIEW')

		query = req.args.get('q')
		try:
			per_page = int(req.args.getfirst('per_page', 
				self.DEFAULT_PER_PAGE))
		except ValueError:
			per_page = self.DEFAULT_PER_PAGE

		try:
			page = int(req.args.getfirst('page', 1))
		except ValueError:
			page = 1

		data = {
			'source': req.args.getlist('source_filters'),
			'author': [auth for auth in req.args.getlist('author') if auth],
			'date_start': req.args.getfirst('date_start'),
			'date_end': req.args.getfirst('date_end'),
			'q': query,
			'start_points': StartPoints.parse_args(req.args, self.providers)
		}

		if not query:
			return self._send_response(req, data)

		# perform query using backend if q is set
		result_map = {}
		total_count = 0
		for provider in self.providers:
			result_count, result_list = provider.query_backend(data)
			total_count += result_count
			result_map[provider.get_name()] = result_list

		data['source_filters'] = self._get_filter_dicts(
			self.SOURCE_FILTERS, 
			req.args
		)
		data['per_page'] = per_page
		data['page'] = page
		results = self._merge_results(result_map, per_page)
		self._add_href_to_results(results)
		data['results'] = Paginator(
			results, 
			page=page-1, 
			max_per_page=per_page, 
			num_items=total_count
		)

		# pagination next/prev links
		if data['results'].has_next_page:
			start_points = StartPoints.format(results, data['start_points'])
			next_href = "javascript:next_page(%s)" % start_points
			add_link(req, 'next', next_href, _('Next Page'))

		if data['results'].has_previous_page:
			prev_href = "javascript:history.go(-1)"
			add_link(req, 'prev', prev_href, _('Previous Page'))
		
		return self._send_response(req, data)

	def _send_response(self, req, data):
		"""Send the response."""

		# look for warnings
		if not len(self.providers):
			add_warning(req, _('No advanced search providers found. ' +
				'You must register a search backend.'))

		if data.get('results') and not len(data['results']):
			add_warning(req, _('No results.'))
			
		add_stylesheet(req, 'common/css/search.css')
		add_stylesheet(req, 'advsearch/css/advsearch.css')
		add_script(req, 'advsearch/js/advsearch.js')
		return 'advsearch.html', data, None

	def _merge_results(self, result_map, per_page):
		"""
		Merge results from multiple sources by score in each result. Return
		the search results to display to the user
		
		Example:
		[
			{
				'title': 'Trac Help', 
				'href': 'http://...', 
				'date': '2011-04-20 12:34:00', 
				'author': 'admin',
				'summary': '...'
			},
			...
		]
		"""
		# add backend_name as a key to each result and merge lists
		all_results = []
		for backend_name, results in result_map.iteritems():
			for result_dict in results:
				result_dict['backend_name'] = backend_name
			all_results.extend(results)
				
		# sort and return results for the page
		all_results.sort(key=itemgetter('score'), reverse=True)
		return all_results[:per_page]

	def _add_href_to_results(self, results):
		"""Add an href key/value to each result dict based on source."""
		for result in results:
			if result['source'] == 'wiki':
				result['href'] = self.env.href.wiki(result['title'])
			# TODO: build href from other sources

	def _get_filter_dicts(self, filter_list, req_args):
		"""Map filters to filter dicts for the frontend."""
		return [
			{'name': filter, 'active': req_args.get(filter)}
			for filter in filter_list
		]

	# ITemplateProvider methods
	def get_htdocs_dirs(self):
		return [('advsearch', pkg_resources.resource_filename(__name__, 'htdocs'))]

	def get_templates_dirs(self):
		return [pkg_resources.resource_filename(__name__, 'templates')]

	# IWikiSyntaxProvider methods
	def get_wiki_syntax(self):
		return []
		
	def get_link_resolvers(self):
		# TODO
		return []


class StartPoints(object):
	"""Format and parse start points for search."""

	FORMAT_STRING = 'provider_start_point:%s'

	@classmethod
	def parse_args(cls, req_args, provider_list):
		"""Return a dict of start points by provider from request args."""
		start_points = {}
		for provider in provider_list:
			start_points[provider.get_name()] = req_args.getfirst(
				cls.FORMAT_STRING % provider.get_name(), 
				0
			)
		return start_points

	@classmethod
	def format(cls, results, prev_start_points):
		"""Return dict of start_point name to value."""
		start_points = {}
		for result in results:
			backend_name = result['backend_name']
			if not backend_name in start_points:
				try:
					prev_start = int(prev_start_points.get(backend_name, 0))
				except:
					prev_start = 0
				start_points[backend_name] = prev_start
			start_points[backend_name] += 1

		return simplejson.dumps(
			[
				{
					'name': cls.FORMAT_STRING % name,
					'value': value
				} 
				for (name, value) in start_points.iteritems()
			]
		)

