# -*- coding: utf-8 -*-
#
# Copyright © 2011 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

import logging

import web
from web.webapi import BadRequest

from pulp.common.tags import action_tag, resource_tag
from pulp.server import config as pulp_config
from pulp.server.auth.authorization import READ, CREATE, UPDATE, DELETE
from pulp.server.db.model.criteria import Criteria
from pulp.server.dispatch import constants as dispatch_constants
from pulp.server.dispatch import factory as dispatch_factory
from pulp.server.dispatch.call import CallRequest, CallReport
from pulp.server.exceptions import InvalidValue, MissingResource, MissingValue
from pulp.server.itineraries.consumer import (
    consumer_content_install_itinerary, consumer_content_uninstall_itinerary,
    consumer_content_update_itinerary)
from pulp.server.itineraries.bind import (
    bind_itinerary, unbind_itinerary, forced_unbind_itinerary)
from pulp.server.managers.consumer.query import ConsumerQueryManager
from pulp.server.db.model.consumer import Bind, RepoProfileApplicability, UnitProfile
from pulp.server.webservices.controllers.base import JSONController
from pulp.server.webservices.controllers.search import SearchController
from pulp.server.webservices.controllers.decorators import auth_required
from pulp.server.webservices import execution
from pulp.server.webservices import serialization
import pulp.server.managers.factory as managers

# -- constants ----------------------------------------------------------------

_LOG = logging.getLogger(__name__)


def expand_consumers(options, consumers):
    """
    Expand a list of users based on flags specified in the
    post body or query parameters.  The _href is always added by the
    serialization function used.
    Supported options:
      details - synonym for: (bindings=True,)
      bindings - include bindings
    @param options: The (expanding) options.
    @type options: dict
    @param consumers: A list of consumers
    @type consumers: list
    @return: A list of expanded consumers.
    @rtype: list
    """
    if options.get('details', False):
        options['bindings'] = True
    # add bindings
    if options.get('bindings', False):
        ids = [c['id'] for c in consumers]
        manager = managers.consumer_bind_manager()
        criteria = Criteria({'consumer_id':{'$in':ids}})
        bindings = manager.find_by_criteria(criteria)
        collated = {}
        for b in bindings:
            lst = collated.setdefault(b['consumer_id'], [])
            lst.append(b)
        for consumer in consumers:
            consumer['bindings'] = \
                [serialization.binding.serialize(b, False)
                    for b in collated.get(consumer['id'], [])]
    return consumers


# -- controllers --------------------------------------------------------------

class Consumers(JSONController):

    @auth_required(READ)
    def GET(self):
        params = web.input()
        manager = managers.consumer_query_manager()
        consumers = expand_consumers(params, manager.find_all())
        for c in consumers:
            href = serialization.link.child_link_obj(c['id'])
            c.update(href)
        return self.ok(consumers)

    @auth_required(CREATE)
    def POST(self):
        body = self.params()
        id = body.get('id')
        display_name = body.get('display_name')
        description = body.get('description')
        notes = body.get('notes')

        manager = managers.consumer_manager()
        args = [id, display_name, description, notes]
        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, id),
                action_tag('create')]

        call_request = CallRequest(manager.register,
                                   args,
                                   weight=weight,
                                   tags=tags)
        call_request.creates_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, id)

        call_report = CallReport.from_call_request(call_request)
        call_report.serialize_result = False

        consumer = execution.execute_sync(call_request, call_report)
        consumer.update({'_href': serialization.link.child_link_obj(consumer['id'])})
        return self.created(consumer['_href'], consumer)

class Consumer(JSONController):

    @auth_required(READ)
    def GET(self, id):
        params = web.input()
        manager = managers.consumer_manager()
        consumer = manager.get_consumer(id)
        consumer = expand_consumers(params, [consumer])[0]
        href = serialization.link.current_link_obj()
        consumer.update(href)
        return self.ok(consumer)

    @auth_required(DELETE)
    def DELETE(self, id):
        manager = managers.consumer_manager()
        tags = [
            resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, id),
            action_tag('delete'),
        ]
        call_request = CallRequest(
            manager.unregister,
            [id],
            tags=tags)
        call_request.deletes_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, id)
        return self.ok(execution.execute(call_request))

    @auth_required(UPDATE)
    def PUT(self, id):
        body = self.params()
        delta = body.get('delta')
        manager = managers.consumer_manager()
        tags = [
            resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, id),
            action_tag('update')
        ]
        call_request = CallRequest(
            manager.update,
            [id, delta],
            tags=tags)
        call_request.updates_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, id)
        consumer = execution.execute(call_request)
        href = serialization.link.current_link_obj()
        consumer.update(href)
        return self.ok(consumer)


class ConsumerSearch(SearchController):

    def __init__(self):
        super(ConsumerSearch, self).__init__(
            managers.consumer_query_manager().find_by_criteria)

    def GET(self):
        params = web.input()
        ignored = ('details', 'bindings')
        found = self._get_query_results_from_get(ignored)
        consumers = expand_consumers(params, found)
        for c in consumers:
            href = serialization.link.search_safe_link_obj(c['id'])
            c.update(href)
        return self.ok(consumers)

    def POST(self):
        body = self.params()
        found = self._get_query_results_from_post()
        consumers = expand_consumers(body, found)
        for c in consumers:
            href = serialization.link.search_safe_link_obj(c['id'])
            c.update(href)
        return self.ok(consumers)



class Bindings(JSONController):
    """
    Consumer bindings represents the collection of
    objects used to associate a consumer and a repo-distributor
    association.  Users wanting to create this association will
    create an object in this collection.  Both bind and unbind
    is idempotent.
    """

    #@auth_required(READ)
    def GET(self, consumer_id, repo_id=None):
        """
        Fetch all bind objects referencing the
        specified I{consumer_id}.
        @param consumer_id: The specified consumer.
        @type consumer_id: str
        @return: A list of bind dict:
            {consumer_id:<str>,
             repo_id:<str>,
             distributor_id:<str>,
             href:<str>,
             type_id:<str>,
             details:<dict>}
        @rtype: dict
        """
        manager = managers.consumer_bind_manager()
        bindings = manager.find_by_consumer(consumer_id, repo_id)
        bindings = [serialization.binding.serialize(b) for b in bindings]
        return self.ok(bindings)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        """
        Create a bind association between the specified
        consumer by id included in the URL path and a repo-distributor
        specified in the POST body: {repo_id:<str>, distributor_id:<str>}.
        Designed to be idempotent so only MissingResource is expected to
        be raised by manager.
        @param consumer_id: The consumer to bind.
        @type consumer_id: str
        @return: The list of call_reports
        @rtype: list
        """
        # validate consumer
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        # get other options and validate them
        body = self.params()
        repo_id = body.get('repo_id')
        distributor_id = body.get('distributor_id')
        binding_config = body.get('binding_config', None)
        options = body.get('options', {})
        notify_agent = body.get('notify_agent', True)

        managers.repo_query_manager().get_repository(repo_id)
        managers.repo_distributor_manager().get_distributor(repo_id, distributor_id)

        # bind
        call_requests = bind_itinerary(consumer_id, repo_id, distributor_id, notify_agent, binding_config, options)
        execution.execute_multiple(call_requests)


class Binding(JSONController):
    """
    Represents a specific bind resource.
    """

    @auth_required(READ)
    def GET(self, consumer_id, repo_id, distributor_id):
        """
        Fetch a specific bind object which represents a specific association
        between a consumer and repo-distributor.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param repo_id: A repo ID.
        @type repo_id: str
        @param distributor_id: A distributor ID.
        @type distributor_id: str
        @return: A specific bind object:
            {consumer_id:<str>,
             repo_id:<str>,
             distributor_id:<str>,
             href:<str>,
             type_id:<str>,
             details:<dict>}
        @rtype: dict
        """
        manager = managers.consumer_bind_manager()
        bind = manager.get_bind(consumer_id, repo_id, distributor_id)
        serialized_bind = serialization.binding.serialize(bind)
        return self.ok(serialized_bind)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, repo_id, distributor_id):
        """
        Delete a bind association between the specified
        consumer and repo-distributor.  Designed to be idempotent.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param repo_id: A repo ID.
        @type repo_id: str
        @param distributor_id: A distributor ID.
        @type distributor_id: str
        @return: The list of call_reports
        @rtype: list
        """
        body = self.params()
        # validate resources
        manager = managers.consumer_bind_manager()
        binding = manager.get_bind(consumer_id, repo_id, distributor_id)
        notify_agent = binding['notify_agent']
        # delete (unbind)
        forced = body.get('force', False)
        options = body.get('options', {})
        if forced or not notify_agent:
            call_requests = forced_unbind_itinerary(
                consumer_id,
                repo_id,
                distributor_id,
                options)
        else:
            call_requests = unbind_itinerary(
                consumer_id,
                repo_id,
                distributor_id,
                options)
        execution.execute_multiple(call_requests)


class BindingSearch(SearchController):
    """
    Bind search.
    """
    def __init__(self):
        SearchController.__init__(self, managers.consumer_bind_manager().find_by_criteria)


class Content(JSONController):
    """
    Represents a specific bind object.
    """

    @auth_required(CREATE)
    def POST(self, id, action):
        """
        Content actions.
        """
        method = getattr(self, action, None)
        if method:
            return method(id)
        else:
            raise BadRequest()

    def install(self, id):
        """
        Install content (units) on a consumer.
        Expected body: {units:[], options:<dict>}
        where unit is: {type_id:<str>, unit_key={}} and the
        options is a dict of install options.
        @param id: A consumer ID.
        @type id: str
        @return: TBD
        @rtype: dict
        """
        body = self.params()
        units = body.get('units')
        options = body.get('options')
        call_request = consumer_content_install_itinerary(id, units, options)[0]
        result = execution.execute_async(self, call_request)
        return result

    def update(self, id):
        """
        Update content (units) on a consumer.
        Expected body: {units:[], options:<dict>}
        where unit is: {type_id:<str>, unit_key={}} and the
        options is a dict of update options.
        @param id: A consumer ID.
        @type id: str
        @return: TBD
        @rtype: dict
        """
        body = self.params()
        units = body.get('units')
        options = body.get('options')
        call_request = consumer_content_update_itinerary(id, units, options)[0]
        result = execution.execute_async(self, call_request)
        return result

    def uninstall(self, id):
        """
        Uninstall content (units) on a consumer.
        Expected body: {units:[], options:<dict>}
        where unit is: {type_id:<str>, unit_key={}} and the
        options is a dict of uninstall options.
        @param id: A consumer ID.
        @type id: str
        @return: TBD
        @rtype: dict
        """
        body = self.params()
        units = body.get('units')
        options = body.get('options')
        call_request = consumer_content_uninstall_itinerary(id, units, options)[0]
        result = execution.execute_async(self, call_request)
        return result

class ConsumerHistory(JSONController):

    @auth_required(READ)
    def GET(self, id):
        """
        @type id: str
        @param id: consumer id
        """
        valid_filters = ['event_type', 'limit', 'sort', 'start_date', 'end_date']
        filters = self.filters(valid_filters)
        event_type = filters.get('event_type', None)
        limit = filters.get('limit', None)
        sort = filters.get('sort', None)
        start_date = filters.get('start_date', None)
        end_date = filters.get('end_date', None)

        if sort is None:
            sort = 'descending'
        else:
            sort = sort[0]

        if limit:
            limit = int(limit[0])

        if start_date:
            start_date = start_date[0]

        if end_date:
            end_date = end_date[0]

        if event_type:
            event_type = event_type[0]

        results = managers.consumer_history_manager().query(consumer_id=id, event_type=event_type, limit=limit,
                                    sort=sort, start_date=start_date, end_date=end_date)
        return self.ok(results)


class Profiles(JSONController):
    """
    Consumer I{profiles} represents the collection of
    objects used to associate consumers and installed content
    unit profiles.
    """

    @auth_required(READ)
    def GET(self, consumer_id):
        """
        Get all profiles associated with a consumer.
        @param consumer_id: The consumer ID.
        @type consumer_id: str
        @return: A list of profiles:
          profile is: {consumer_id:<str>, content_type:<str>, profile:<dict>}
        @return: list
        """
        manager = managers.consumer_profile_manager()
        profiles = manager.get_profiles(consumer_id)
        profiles = [serialization.consumer.profile(p) for p in profiles]
        return self.ok(profiles)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        """
        Associate a profile with a consumer by content type ID.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @return: The created model object:
            {consumer_id:<str>, content_type:<str>, profile:<dict>}
        @rtype: dict
        """
        body = self.params()
        content_type = body.get('content_type')
        profile = body.get('profile')

        manager = managers.consumer_profile_manager()
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_CONTENT_UNIT_TYPE, content_type),
                action_tag('profile_create')]

        call_request = CallRequest(manager.create,
                                   [consumer_id, content_type],
                                   {'profile': profile},
                                   tags=tags,
                                   weight=0,
                                   kwarg_blacklist=['profile'])
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        call_report = CallReport.from_call_request(call_request)
        call_report.serialize_result = False

        consumer = execution.execute_sync(call_request, call_report)
        link = serialization.link.child_link_obj(consumer_id, content_type)
        consumer.update(link)

        return self.created(link['_href'], consumer)


class Profile(JSONController):
    """
    Consumer I{profiles} represents the collection of
    objects used to associate consumers and installed content
    unit profiles.
    """

    @auth_required(READ)
    def GET(self, consumer_id, content_type):
        """
        @param consumer_id: The consumer ID.
        @type consumer_id: str
        """
        manager = managers.consumer_profile_manager()
        profile = manager.get_profile(consumer_id, content_type)
        serialized = serialization.consumer.profile(profile)
        return self.ok(serialized)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, content_type):
        """
        Update the association of a profile with a consumer by content type ID.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param content_type: A content unit type ID.
        @type content_type: str
        @return: The updated model object:
            {consumer_id:<str>, content_type:<str>, profile:<dict>}
        @rtype: dict
        """
        body = self.params()
        profile = body.get('profile')

        manager = managers.consumer_profile_manager()
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_CONTENT_UNIT_TYPE, content_type),
                action_tag('profile_update')]

        call_request = CallRequest(manager.update,
                                   [consumer_id, content_type],
                                   {'profile': profile},
                                   tags=tags,
                                   weight=0,
                                   kwarg_blacklist=['profile'])
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        call_report = CallReport.from_call_request(call_request)
        call_report.serialize_result = False

        consumer = execution.execute_sync(call_request, call_report)
        link = serialization.link.child_link_obj(consumer_id, content_type)
        consumer.update(link)

        return self.ok(consumer)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, content_type):
        """
        Delete an association between the specified
        consumer and profile.  Designed to be idempotent.
        @param consumer_id: A consumer ID.
        @type consumer_id: str
        @param content_type: The content type ID.
        @type content_type: str
        @return: The deleted model object:
            {consumer_id:<str>, content_type:<str>, profile:<dict>}
            Or, None if bind does not exist.
        @rtype: dict
        """
        manager = managers.consumer_profile_manager()
        args = [
            consumer_id,
            content_type,
        ]
        tags = [
            resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
        ]
        call_request = CallRequest(manager.delete,
                                   args=args,
                                   tags=tags)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        return self.ok(execution.execute(call_request))


class ContentApplicability(JSONController):
    """
    Query content applicability.
    """
    @auth_required(READ)
    def POST(self):
        """
        Query content applicability for a given consumer criteria query.

        body {consumer_criteria: <dict>,
              content_types: <list>[optional]}

        This method returns a list of dictionaries that each have two keys: 'consumers', and
        'applicability'. 'consumers' will index a list of consumer_ids, for consumers that have
        the same repository bindings and profiles. 'applicability' will index a dictionary
        structure that will have keys for each content type that is applicable, and the content
        type ids will index the applicability data for those content types. For example,

        [{'consumers': ['consumer_1', 'consumer_2'],
          'applicability': {'content_type_1': ['unit_1', 'unit_3']}},
         {'consumers': ['consumer_2', 'consumer_3'],
          'applicability': {'content_type_1': ['unit_1', 'unit_2']}}]

        :return: applicability data matching the consumer criteria query
        :rtype:  list
        """
        # Get the consumer_ids that match the consumer criteria query that the requestor queried
        # with, and build a map from consumer_id to a dict with profiles and repo_ids for each
        # consumer
        try:
            consumer_ids = self._get_consumer_ids()
            content_types = self._get_content_types()
        except InvalidValue, e:
            return self.bad_request(str(e))
        consumer_map = dict([(c, {'profiles': [], 'repo_ids': []}) for c in consumer_ids])

        # Fill out the mapping of consumer_ids to profiles, and store the list of profile_hashes
        profile_hashes = self._add_profiles_to_consumer_map_and_get_hashes(consumer_ids,
                                                                           consumer_map)

        # Now add in repo_ids that the consumers are bound to
        self._add_repo_ids_to_consumer_map(consumer_ids, consumer_map)
        # We don't need the list of consumer_ids anymore, so let's free a little RAM
        del consumer_ids

        # Now lets get all RepoProfileApplicability objects that have the profile hashes for our
        # consumers
        applicability_map = self._get_applicability_map(profile_hashes, content_types)
        # We don't need the profile_hashes anymore, so let's free some RAM
        del profile_hashes

        # Now we need to add consumers who match the applicability data to the applicability_map
        self._add_consumers_to_applicability_map(consumer_map, applicability_map)
        # We don't need the consumer_map anymore, so let's free it up
        del consumer_map

        # Collate all the entries for the same sets of consumers together
        consumer_applicability_map = self._get_consumer_applicability_map(applicability_map)
        # Free the applicability_map, we don't need it anymore
        del applicability_map

        # Form the data into the expected output format and return
        return self.ok(self._format_report(consumer_applicability_map))

    @staticmethod
    def _add_consumers_to_applicability_map(consumer_map, applicability_map):
        """
        For all consumers in the consumer_map, look for their profiles and repos in the
        applicability_map, and if found, add the consumer_ids to the applicability_map.

        :param consumer_map:      A dictionary mapping consumer_ids to dictionaries with keys
                                  'profiles' and 'repo_ids'. 'profiles' indexes a list of profiles
                                  for each consumer_id, and 'repo_ids' indexes a list of repo_ids
                                  that the consumer is bound to.
        :type  consumer_map:      dict
        :param applicability_map: The mapping of (profile_hash, repo_id) to applicability_data and
                                  consumer_ids the data applies to. This method appends
                                  consumer_ids to the appropriate lists of consumer_ids
        :type  applicability_map: dict
        """
        for consumer_id, repo_profile_data in consumer_map.items():
            for profile in repo_profile_data['profiles']:
                for repo_id in repo_profile_data['repo_ids']:
                    repo_profile = (profile['profile_hash'], repo_id)
                    # Only add the consumer to the applicability map if there is applicability_data
                    # for this combination of repository and profile
                    if repo_profile in applicability_map:
                        applicability_map[repo_profile]['consumers'].append(consumer_id)

    @staticmethod
    def _add_profiles_to_consumer_map_and_get_hashes(consumer_ids, consumer_map):
        """
        Query for all the profiles associated with the given list of consumer_ids, add those
        profiles to the consumer_map, and then return a list of all profile_hashes.

        :param consumer_ids: A list of consumer_ids that we want to map the profiles to
        :type  consumer_ids: list
        :param consumer_map: A dictionary mapping consumer_ids to a dictionary with key 'profiles',
                             which indexes a list that this method will append the found profiles
                             to.
        :type  consumer_map: dict
        :return:             A list of the profile_hashes that were associated with the given
                             consumers
        :rtype:              list
        """
        profiles = UnitProfile.get_collection().find(
            {'consumer_id': {'$in': consumer_ids}},
            fields=['consumer_id', 'profile_hash'])
        profile_hashes = set()
        for p in profiles:
            consumer_map[p['consumer_id']]['profiles'].append(p.to_dict())
            profile_hashes.add(p['profile_hash'])
        # Let's return a list of all the unique profile_hashes for the query we will do a
        # bit later for applicability data
        return list(profile_hashes)

    @staticmethod
    def _add_repo_ids_to_consumer_map(consumer_ids, consumer_map):
        """
        Query for all bindings for the given list of consumer_ids, and for each one add the bound
        repo_ids to the consumer_map's entry for the consumer.

        :param consumer_ids: The list of consumer_ids. We could pull this from the consumer_map,
                             but since we already have this list it's probably more performant to
                             use it as is.
        :type  consumer_ids: list
        :param consumer_map: A dictionary mapping consumer_ids to a dictionary with key 'profiles',
                             which indexes a list that this method will append the found profiles
                             to.
        :type  consumer_map: dict
        """
        bindings = Bind.get_collection().find(
            {'consumer_id': {'$in': consumer_ids}},
            fields=['consumer_id', 'repo_id'])
        for b in bindings:
            consumer_map[b['consumer_id']]['repo_ids'].append(b['repo_id'])

    @staticmethod
    def _format_report(consumer_applicability_map):
        """
        Turn the consumer_applicability_map into the expected response format for this API call.

        :param consumer_applicability_map: A mapping of frozensets of consumers to their
                                           applicability data
        :type  consumer_applicability_map: dict
        :return:                           A list of dictionaries that have two keys, consumers
                                           and applicability. consumers indexes a list of
                                           consumer_ids, and applicability indexes the
                                           applicability data for those consumer_ids.
        :rtype:                            list
        """
        report = []
        for consumers, applicability in consumer_applicability_map.items():
            # If there are no consumers for this applicability data, there is no need to include
            # it in the report
            if consumers:
                applicability_data = {'consumers': list(consumers),
                                      'applicability': applicability}
                report.append(applicability_data)

        return report

    @staticmethod
    def _get_applicability_map(profile_hashes, content_types):
        """
        Build an "applicability_map", which is a dictionary that maps tuples of
        (profile_hash, repo_id) to a dictionary of applicability data and consumer_ids. The
        consumer_ids are just initialized to an empty list, so that a later method can add
        consumers to it. For example, it might look like:

        {('profile_hash_1', 'repo_1'): {'applicability': {<applicability_data>}, 'consumers': []}}

        :param profile_hashes: A list of profile hashes that the applicabilities should be queried
                               with. The applicability map is initialized with all applicability
                               data for all the given profile_hashes.
        :type  profile_hashes: list
        :param content_types:  If not None, content_types is a list of content_types to
                               be included in the applicability data within the
                               applicability_map
        :type  content_types:  list or None
        :return:               The applicability map
        :rtype:                dict
        """
        applicabilities = RepoProfileApplicability.get_collection().find(
            {'profile_hash': {'$in': profile_hashes}},
            fields=['profile_hash', 'repo_id', 'applicability'])
        return_value = {}
        for a in applicabilities:
            if content_types is not None:
                # The caller has requested us to filter by content_type, so we need to look through
                # the applicability data and filter out the unwanted content types. Some
                # applicabilities may end up being empty if they don't have any data for the
                # requested types, so we'll build a list of those to remove
                for key in a['applicability'].keys():
                    if key not in content_types:
                        del a['applicability'][key]
                # If a doesn't have anything worth reporting, move on to the next applicability
                if not a['applicability']:
                    continue
            return_value[(a['profile_hash'], a['repo_id'])] = {'applicability': a['applicability'],
                                                               'consumers': []}
        return return_value

    @staticmethod
    def _get_consumer_applicability_map(applicability_map):
        """
        Massage the applicability_map into a form that will help us to collate applicability
        groups that contain the same data together.

        :param applicability_map: The mapping of (profile_hash, repo_id) to applicability_data and
                                  consumer_ids it applies to. This method appends consumer_ids to
                                  the appropriate lists of consumer_ids
        :type  applicability_map: dict
        :return:                  The consumer_applicability_map, which maps frozensets of
                                  consumer_ids to their collective applicability data.
        :rtype:                   dict
        """
        consumer_applicability_map = {}
        for repo_profile, data in applicability_map.items():
            # This will be the key for our map, a set of the consumers that share data
            consumers = frozenset(data['consumers'])
            if consumers in consumer_applicability_map:
                for content_type, applicability in data['applicability'].items():
                    if content_type in consumer_applicability_map[consumers]:
                        # There is already applicability data for this consumer set and
                        # content type. We will convert the existing data and the new data to
                        # sets, generate the union of those sets, and turn it back into a list
                        # so that we can report unique units.
                        consumer_applicability_map[consumers][content_type] = list(
                            set(consumer_applicability_map[consumers][content_type]) |\
                            set(applicability))
                    else:
                        consumer_applicability_map[consumers][content_type] = applicability
            else:
                consumer_applicability_map[consumers] = data['applicability']
        return consumer_applicability_map

    def _get_consumer_ids(self):
        """
        Process the POST data, finding the consumer_criteria given by the user, and resolve it to a
        list of consumer_ids.

        :return: A list of consumer_ids for which we need to return applicability data
        :rtype:  list
        """
        body = self.params()

        try:
            consumer_criteria = body.get('consumer_criteria')
        except AttributeError:
            raise InvalidValue('The input to this method must be a JSON document with a '
                               "'consumer_criteria' key.")
        consumer_criteria = Criteria.from_client_input(consumer_criteria)
        # We only need the consumer ids
        consumer_criteria['fields'] = ['id']
        return [c['id'] for c in ConsumerQueryManager.find_by_criteria(consumer_criteria)]

    def _get_content_types(self):
        """
        Get the list of content_types that the caller wishes to limit the response to. If the
        caller did not include content types, this will return None.

        :return: The list of content_types that the applicability query should be limited to,
                 or None if not specified
        :rtype:  list or None
        """
        body = self.params()

        content_types = body.get('content_types', None)
        if content_types is not None and not isinstance(content_types, list):
            raise InvalidValue('content_types must index a list.')

        return content_types


class UnitInstallScheduleCollection(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        consumer_tag = resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        install_tag = action_tag('scheduled_unit_install')

        scheduler = dispatch_factory.scheduler()
        scheduled_calls = scheduler.find(consumer_tag, install_tag)

        schedule_objs = []
        for call in scheduled_calls:
            obj = serialization.dispatch.scheduled_unit_management_obj(call)
            obj.update(serialization.link.child_link_obj(obj['_id']))
            schedule_objs.append(obj)
        return self.ok(schedule_objs)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        units = schedule_data.pop('units', None)
        install_options = {'options': schedule_data.pop('options', {})}

        if not units:
            raise MissingValue(['units'])

        schedule_manager = managers.schedule_manager()

        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                action_tag('create_unit_install_schedule')]

        call_request = CallRequest(schedule_manager.create_unit_install_schedule,
                                   [consumer_id, units, install_options, schedule_data],
                                   weight=weight,
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        schedule_id = execution.execute_sync(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.child_link_obj(schedule_id))
        return self.created(scheduled_obj['_href'], scheduled_obj)


class UnitInstallScheduleResource(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        if consumer_id not in scheduled_call['call_request'].args:
            raise MissingResource(consumer=consumer_id, unit_install_schedule=schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        install_options = None
        units = schedule_data.pop('units', None)

        if 'options' in schedule_data:
            install_options = {'options': schedule_data.pop('options')}

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('update_unit_install_schedule')]

        call_request = CallRequest(schedule_manager.update_unit_install_schedule,
                                   [consumer_id, schedule_id, units, install_options, schedule_data],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.updates_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        execution.execute(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('delete_unit_install_schedule')]

        call_request = CallRequest(schedule_manager.delete_unit_install_schedule,
                                   [consumer_id, schedule_id],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.deletes_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        result = execution.execute(call_request)
        return self.ok(result)


class UnitUpdateScheduleCollection(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        consumer_tag = resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        update_tag = action_tag('scheduled_unit_update')

        scheduler = dispatch_factory.scheduler()
        scheduled_calls = scheduler.find(consumer_tag, update_tag)

        schedule_objs = []
        for call in scheduled_calls:
            obj = serialization.dispatch.scheduled_unit_management_obj(call)
            obj.update(serialization.link.child_link_obj(obj['_id']))
            schedule_objs.append(obj)
        return self.ok(schedule_objs)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        units = schedule_data.pop('units', None)
        update_options = {'options': schedule_data.pop('options', {})}

        if not units:
            raise MissingValue(['units'])

        schedule_manager = managers.schedule_manager()

        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                action_tag('create_unit_update_schedule')]

        call_request = CallRequest(schedule_manager.create_unit_update_schedule,
                                   [consumer_id, units, update_options, schedule_data],
                                   weight=weight,
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        schedule_id = execution.execute_sync(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.child_link_obj(schedule_id))
        return self.created(scheduled_obj['_href'], scheduled_obj)


class UnitUpdateScheduleResource(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        if consumer_id not in scheduled_call['call_request'].args:
            raise MissingResource(consumer=consumer_id, unit_update_schedule=schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        install_options = None
        units = schedule_data.pop('units', None)

        if 'options' in schedule_data:
            install_options = {'options': schedule_data.pop('options')}

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('update_unit_update_schedule')]

        call_request = CallRequest(schedule_manager.update_unit_update_schedule,
                                   [consumer_id, schedule_id, units, install_options, schedule_data],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.updates_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        execution.execute(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('delete_unit_update_schedule')]

        call_request = CallRequest(schedule_manager.delete_unit_update_schedule,
                                   [consumer_id, schedule_id],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.deletes_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)
        result = execution.execute(call_request)
        return self.ok(result)


class UnitUninstallScheduleCollection(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        consumer_tag = resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        uninstall_tag = action_tag('scheduled_unit_uninstall')

        scheduler = dispatch_factory.scheduler()
        scheduled_calls = scheduler.find(consumer_tag, uninstall_tag)

        schedule_objs = []
        for call in scheduled_calls:
            obj = serialization.dispatch.scheduled_unit_management_obj(call)
            obj.update(serialization.link.child_link_obj(obj['_id']))
            schedule_objs.append(obj)
        return self.ok(schedule_objs)

    @auth_required(CREATE)
    def POST(self, consumer_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        units = schedule_data.pop('units', None)
        uninstall_options = {'options': schedule_data.pop('options', {})}

        if not units:
            raise MissingValue(['units'])

        schedule_manager = managers.schedule_manager()

        weight = pulp_config.config.getint('tasks', 'create_weight')
        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                action_tag('create_unit_uninstall_schedule')]

        call_request = CallRequest(schedule_manager.create_unit_uninstall_schedule,
                                   [consumer_id, units, uninstall_options, schedule_data],
                                   weight=weight,
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)

        schedule_id = execution.execute_sync(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.child_link_obj(schedule_id))
        return self.created(scheduled_obj['_href'], scheduled_obj)


class UnitUninstallScheduleResource(JSONController):

    @auth_required(READ)
    def GET(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        if consumer_id not in scheduled_call['call_request'].args:
            raise MissingResource(consumer=consumer_id, unit_uninstall_schedule=schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(UPDATE)
    def PUT(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_data = self.params()
        install_options = None
        units = schedule_data.pop('units', None)

        if 'options' in schedule_data:
            install_options = {'options': schedule_data.pop('options')}

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('update_unit_uninstall_schedule')]

        call_request = CallRequest(schedule_manager.update_unit_uninstall_schedule,
                                   [consumer_id, schedule_id, units, install_options, schedule_data],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.updates_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        execution.execute(call_request)

        scheduler = dispatch_factory.scheduler()
        scheduled_call = scheduler.get(schedule_id)

        scheduled_obj = serialization.dispatch.scheduled_unit_management_obj(scheduled_call)
        scheduled_obj.update(serialization.link.current_link_obj())
        return self.ok(scheduled_obj)

    @auth_required(DELETE)
    def DELETE(self, consumer_id, schedule_id):
        consumer_manager = managers.consumer_manager()
        consumer_manager.get_consumer(consumer_id)

        schedule_manager = managers.schedule_manager()

        tags = [resource_tag(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id),
                resource_tag(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id),
                action_tag('delete_unit_uninstall_schedule')]

        call_request = CallRequest(schedule_manager.delete_unit_uninstall_schedule,
                                   [consumer_id, schedule_id],
                                   tags=tags,
                                   archive=True)
        call_request.reads_resource(dispatch_constants.RESOURCE_CONSUMER_TYPE, consumer_id)
        call_request.deletes_resource(dispatch_constants.RESOURCE_SCHEDULE_TYPE, schedule_id)

        result = execution.execute(call_request)
        return self.ok(result)

# -- web.py application -------------------------------------------------------

urls = (
    '/$', Consumers,
    '/search/$', ConsumerSearch,
    '/binding/search/$', BindingSearch,
    '/actions/content/applicability/$', ContentApplicability,
    '/([^/]+)/bindings/$', Bindings,
    '/([^/]+)/bindings/([^/]+)/$', Bindings,
    '/([^/]+)/bindings/([^/]+)/([^/]+)/$', Binding,
    '/([^/]+)/profiles/$', Profiles,
    '/([^/]+)/profiles/([^/]+)/$', Profile,
    '/([^/]+)/schedules/content/install/', UnitInstallScheduleCollection,
    '/([^/]+)/schedules/content/install/([^/]+)/', UnitInstallScheduleResource,
    '/([^/]+)/schedules/content/update/', UnitUpdateScheduleCollection,
    '/([^/]+)/schedules/content/update/([^/]+)/', UnitUpdateScheduleResource,
    '/([^/]+)/schedules/content/uninstall/', UnitUninstallScheduleCollection,
    '/([^/]+)/schedules/content/uninstall/([^/]+)/', UnitUninstallScheduleResource,
    '/([^/]+)/actions/content/(install|update|uninstall)/$', Content,
    '/([^/]+)/history/$', ConsumerHistory,
    '/([^/]+)/$', Consumer,
)

application = web.application(urls, globals())
