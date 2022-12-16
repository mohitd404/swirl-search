'''
@author:     Sid Probstein
@contact:    sid@swirl.today
'''

import logging as logger
from datetime import datetime
import time

from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings

from swirl.models import Search, SearchProvider, Result
from swirl.tasks import federate_task, rescore_task
from swirl.processors import *

SWIRL_OBJECT_LIST = SearchProvider.QUERY_PROCESSOR_CHOICES + SearchProvider.RESULT_PROCESSOR_CHOICES + Search.PRE_QUERY_PROCESSOR_CHOICES + Search.POST_RESULT_PROCESSOR_CHOICES

SWIRL_OBJECT_DICT = {}
for t in SWIRL_OBJECT_LIST:
    SWIRL_OBJECT_DICT[t[0]]=eval(t[0])

##################################################
##################################################

module_name = 'search.py'

def search(id):

    '''
    Execute the search task workflow
    '''

    # TO DO: support UPDATE_SEARCH!!!
    # assemble provider list - below looks ok
    # skip pre-query processing
    # handle new results/updating in connector++

    start_time = time.time()

    try:
        search = Search.objects.get(id=id)
    except ObjectDoesNotExist as err:
        logger.error(f'{module_name}: Error: ObjectDoesNotExist: {err}')
        return False
    if search.status != 'NEW_SEARCH':
        logger.warning(f"{module_name}: search {search.id} has status {search.status}; set it to NEW_SEARCH to (re)start it")
        return False
    search.status = 'PRE_PROCESSING'
    search.save()
    # check for provider specification
    # security review for 1.7 - OK - filtered by owner
    providers = SearchProvider.objects.filter(active=True, owner=search.owner) | SearchProvider.objects.filter(active=True, shared=True)
    new_provider_list = []
    if search.searchprovider_list:
        # add providers to list by id, name or tag
        for provider in providers:
            if str(provider.id) in search.searchprovider_list:
                new_provider_list.append(provider)
            if provider.name.lower() in (str(p).lower() for p in search.searchprovider_list):
                if not provider in new_provider_list:
                    new_provider_list.append(provider)
            if provider.tags:
                for tag in provider.tags:
                    if tag.lower() in (str(p).lower() for p in search.searchprovider_list):
                        if not provider in new_provider_list:
                            new_provider_list.append(provider)
                # end if
            # end for
        # end for
    else:
        # no provider list
        for provider in providers:
            # active status is determined later on
            if provider.default:
                new_provider_list.append(provider)
    # end if
    providers = new_provider_list
    if len(providers) == 0:
        logger.error(f"{module_name}: error: no SearchProviders configured")
        search.status = 'ERR_NO_SEARCHPROVIDERS'
        search.date_updated = datetime.now()
        search.save()
        return False

    ########################################
    # pre-query processing, which updates query_string_processed
    if search.pre_query_processor:
        search.status = 'PRE_QUERY_PROCESSING'
        search.save()
        try:
            pre_query_processor = eval(search.pre_query_processor, {"search.pre_query_processor": search.pre_query_processor, "__builtins__": None}, SWIRL_OBJECT_DICT)(search.query_string)
            if pre_query_processor.validate():
                search.query_string_processed = pre_query_processor.process()
            else:
                message = f'Error: pre_query_processor.validate() failed'
                logger.error(f'{module_name}: {message}')
                return False
            # end if
        except NameError as err:
            message = f'Error: NameError: {err}'
            logger.error(f'{module_name}: {message}')
            return False
        except TypeError as err:
            message = f'Error: TypeError: {err}'
            logger.error(f'{module_name}: {message}')
            return False
        if search.query_string_processed != search.query_string:
            message = f"Pre-query processing by {search.pre_query_processor} rewrote query_string to: {search.query_string_processed}"
            messages = search.messages
            messages.append(message)
            search.messages = messages
    else:
        search.query_string_processed = search.query_string
    # end if
    
    # to do: use chord()
    ########################################
    search.status = 'FEDERATING'
    search.save()        
    federation_result = {}
    federation_status = {}
    at_least_one = False
    for provider in providers:
        at_least_one = True
        federation_status[provider.id] = None
        logger.debug(f"{module_name}: federate: {search}, {provider}")
        federation_result[provider.id] = federate_task.delay(search.id, provider.id, provider.connector)
    # end for
    if not at_least_one:
        logger.warning(f"{module_name}: no active searchprovider specified: {search.searchprovider_list}")
        search.status = 'ERR_NO_ACTIVE_SEARCHPROVIDERS'
        search.save()
        return False
    # end if
    ########################################
    # asynchronously collect results
    time.sleep(2)
    ticks = 0
    error_flag = False
    at_least_one = False
    while 1:        
        logger.debug(f"{module_name}: tick!")
        # get the list of result objects
        # security review for 1.7 - OK - filtered by search object
        results = Result.objects.filter(search_id=search.id)
        if len(results) == len(providers):
            # every provider has written a result object - exit
            logger.warning(f"{module_name}: all results received, search {search.id}")
            break
        if len(results) > 0:
            at_least_one = True
        ticks = ticks + 1
        search.status = f'FEDERATING_WAIT_{ticks}'
        search.save()    
        time.sleep(1)
        if (ticks + 2) > int(settings.SWIRL_TIMEOUT):
            logger.warning(f"{module_name}: timeout, search {search.id}")
            failed_providers = []
            responding_provider_names = []
            for result in results:
                responding_provider_names.append(result.searchprovider)
            # fixed: don't report in-active providers as failed (above by filtering providers to active=True)
            for provider in providers:
                if not provider.name in responding_provider_names:
                    failed_providers.append(provider.name)
                    error_flag = True
                    logger.warning(f"{module_name}: timeout waiting for: {failed_providers}")
                    message = f"{module_name}: No response from provider: {failed_providers}"
                    search.messages.append(message)
                    search.save()
                # end if
            # end for
            # exit the loop
            break
    # end while
    ########################################
    # update query status
    logger.debug(f"{module_name}: exiting...")
    if error_flag:
        if at_least_one:
            search.status = 'PARTIAL_RESULTS'
        else:
            search.status = 'NO_RESULTS'
        # end if
    else:
        search.status = 'FULL_RESULTS'
    ########################################
    # fix the result url
    # to do: figure out a better solution P1
    search.result_url = f"{settings.PROTOCOL}://{settings.HOSTNAME}:8000/swirl/results?search_id={search.id}&result_mixer={search.result_mixer}"
    # note the sort
    if search.sort.lower() == 'date':
        message = f"Requested sort_by_date from all providers"
        search.messages.append(message)
    search.save()
    logger.debug(f"{module_name}: landed data!")
    ########################################
    # post_result_processing
    if search.post_result_processor:
        last_status = search.status
        search.status = 'POST_RESULT_PROCESSING'
        search.save()
        try:
            post_result_processor = eval(search.post_result_processor, {"search.post_result_processor": search.post_result_processor, "__builtins__": None}, SWIRL_OBJECT_DICT)(search.id)
            if post_result_processor.validate():
                results_modified = post_result_processor.process()
            else:
                message = f'Error: post_result_processor.validate() failed'
                logger.error(f'{module_name}: {message}')
                return False
            # end if
        except NameError as err:
            message = f'Error: NameError: {err}'
            logger.error(f'{module_name}: {message}')
            return False
        except TypeError as err:
            message = f'Error: TypeError: {err}'
            logger.error(f'{module_name}: {message}')
            return False
        message = f"Post processing of results by {search.post_result_processor} updated {results_modified} results"
        search.messages.append(message)    
        search.status = last_status
    if search.status == 'PARTIAL_RESULTS':
        search.status = 'PARTIAL_RESULTS_READY'
    if search.status == 'FULL_RESULTS':
        search.status = 'FULL_RESULTS_READY'
    end_time = time.time()
    search.time = f"{(end_time - start_time):.1f}"
    # message = f"Total search time: {search.time:.1f} (s)"
    # search.messages.append(message)
    search.save()    
    # logger.debug(f"{module_name}: {search.id}, {search.status}")

    return True

##################################################

def rescore(id):

    '''
    Execute the rescore task workflow
    '''

    try:
        search = Search.objects.get(id=id)
        # security review for 1.7 - OK - filtered by search object
        results = Result.objects.filter(search_id=search.id)
    except ObjectDoesNotExist as err:
        logger.error(f'{module_name}: Error: ObjectDoesNotExist: {err}')
        return False

    last_status = search.status
    if not (search.status.endswith('_READY') or search.status == 'RESCORING'):
        logger.warning(f"{module_name}: search {search.id} has status {search.status}, rescore may not work")
        last_status = None

    if len(results) == 0:
        logger.error(f"{module_name}: search {search.id} has no results to rescore")
        return False

    search.status = 'RESCORING'
    search.save()

    if search.post_result_processor:
        try:
            post_result_processor = eval(search.post_result_processor, {"search.post_result_processor": search.post_result_processor, "__builtins__": None}, SWIRL_OBJECT_DICT)(search.id)
            if post_result_processor.validate():
                results_modified = post_result_processor.process()
            else:
                message = f'Error: post_result_processor.validate() failed'
                logger.error(f'{module_name}: {message}')
                return False
            # end if
        except NameError as err:
            message = f'Error: NameError: {err}'
            logger.error(f'{module_name}: {message}')
            return False
        except TypeError as err:
            message = f'Error: TypeError: {err}'
            logger.error(f'{module_name}: {message}')
            return False
        message = f"Rescoring by {search.post_result_processor} updated {results_modified} results on {datetime.now()}"
        search.messages = []
        search.messages.append(message)    
        if last_status:
            search.status = last_status
        else:
            search.status = "FULL_RESULTS_READY"
        search.save()
        return True
    else:
        logger.error(f"{module_name}: search {search.id} has no post_result_processor defined")
        return False
