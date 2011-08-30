import datetime
import json
import logging
import iso8601

from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.template.loader import render_to_string
from django.http import HttpResponse

from lizard_fewsjdbc.layers import FewsJdbc
from lizard_map.daterange import current_start_end_dates
from lizard_rainapp.calculations import herhalingstijd

from nens_graph.rainapp import RainappGraph

logger = logging.getLogger(__name__)


class RainAppAdapter(FewsJdbc):
    """
    Adapter for Rain app.

    identifier: {'location': <locationid>}
    """

    def _get_location_name(self, identifier):
        """Return location_name for identifier."""
        named_locations = self._locations()
        location_id = identifier['location']
        location_name = [
            location['location'] for location in named_locations
            if location['locationid'] == location_id][0]

        return location_name

    def _get_bar_width(self, values, unit, graph):
        """
        Calculates the width of the bar.
        """
        if unit == 'mm/24hr':
            return graph.width / len(values) / 500
        elif unit == 'mm/3hr':
            return graph.width / len(values) / 700
        elif unit == 'mm/hr':
            return graph.width / len(values) / 800
        elif unit == 'mm/5min':
            return graph.width / len(values) / 1000
        else:
            return 0.8

    def bar_image(self, identifiers, start_date, end_date, width, height,
    layout_extra=None):
        """Implement bar_image.

        gebruik self.values(identifier, start_date, end_date) en/of
        self.value_aggregate_default(...) -> see lizard_map.workspace

        """
        today = datetime.datetime.now()
        graph = RainappGraph(start_date, end_date,
                      width=width, height=height, today=today)
        # Gets timeseries, draws the bars, sets  the legend
        for identifier in identifiers:
            location_name = self._get_location_name(identifier)
            cached_value_result = self._cached_values(identifier,
                                                      start_date,
                                                      end_date)
            dates = [row['datetime'] for row in cached_value_result]
            values = [row['value'] for row in cached_value_result]
            units = [row['unit'] for row in cached_value_result]
            unit = ''
            if len(units) > 0:
                unit = units[0]
            if values:
                graph.axes.bar(dates, values, lw=1,
                               edgecolor='blue',
                               width=self._get_bar_width(values, unit, graph),
                               label=location_name)
            graph.set_ylabel(unit)
            graph.legend()
            # Uses first identifier and breaks the loop
            break

        graph.responseobject = HttpResponse(content_type='image/png')
        return graph.png_response()

    def image(self, identifiers, start_date, end_date, width, height,
    layout_extra=None):
        """Return imagedata.Implement bar_image."""
        today = datetime.datetime.now()
        graph = RainappGraph(start_date, end_date,
                      width=width, height=height, today=today)

        for identifier in identifiers:
            location_name = self._get_location_name(identifier)
            cached_value_result = self._cached_values(identifier,
                                                      start_date,
                                                      end_date)
            dates = [row['datetime'] for row in cached_value_result]
            values = [row['value'] for row in cached_value_result]
            units = [row['unit'] for row in cached_value_result]
            unit = ''
            if len(units) > 0:
                unit = units[0]
            if values:
                graph.axes.plot(dates, values,
                                lw=1,
                                label=location_name)
            graph.set_ylabel(unit)
            graph.legend()

        graph.responseobject = HttpResponse(content_type='image/png')
        return graph.png_response()

    def _tzaware(self, dt, tzinfo):
        """Make datetime timezone aware"""
        return datetime.datetime(
            dt.year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second,
            tzinfo=tzinfo)

    def _cached_values(self, identifier, start_date, end_date):
        """
        Same as self.values, but cached.

        The stored values are rounded in days, a 'little bit
        more'. Else the cache will always miss.
        """
        start_date_cache = datetime.datetime(
            start_date.year, start_date.month, start_date.day)
        end_date_cache = (
            datetime.datetime(
                end_date.year, end_date.month, end_date.day) +
                datetime.timedelta(days=1))

        cache_key = hash('%s::%s::%s::%s::%s::%s' % (
                self.jdbc_source.id, self.filterkey, self.parameterkey,
                identifier['location'], start_date_cache, end_date_cache))
        # Datetimes are in string and stored in datetime_str.
        logger.debug('Trying cache...')
        values = cache.get(cache_key)
        if values is None:
            logger.debug('Caching values for %s' % identifier['location'])
            values = self.values(identifier, start_date, end_date)
            # Convert datetimes to strings, the
            # iso8601.iso8601.FixedOffset will not de-pickle.
            for value in values:
                value['datetime_str'] = value['datetime'].isoformat()
                del value['datetime']
            cache.set(cache_key, values, 5 * 60)
            logger.debug('Cache written')
        else:
            logger.debug('Got timeseries from cache')

        # Convert datetime strings to datetime in values
        for value in values:
            value['datetime'] = iso8601.parse_date(value['datetime_str'])
            del value['datetime_str']

        start_date = self._tzaware(start_date, values[0]['datetime'].tzinfo)
        end_date = self._tzaware(end_date, values[0]['datetime'].tzinfo)
        # Remove datetimes out of range.
        while values and values[0]['datetime'] < start_date:
            del values[0]
        while values and values[-1]['datetime'] > end_date:
            del values[-1]

        return values

    def _max_values(self, values, td, start_date, end_date):
        """
        """
        max_values = []

        # Loop values and calc max for each period. Slow, but it works.

        # End_date often ends with 23:59:59, we want to include at
        # least 1 day in case td=1 day, thus the 2 seconds.
        end = end_date - td + datetime.timedelta(seconds=2)
        period_counter = start_date
        one_hour = datetime.timedelta(seconds=3600)

        # Fast way to calculate sum values.
        len_values = len(values)
        min_index, max_index = 0, 0
        sum_values = 0
        while period_counter < end:
            period_start = period_counter
            period_end = period_counter + td

            # Calculate value by subtracting value(s) from front and
            # adding new value(s) from end.
            while (max_index + 1 < len_values and
                   values[max_index+1]['datetime'] < period_end):

                sum_values += values[max_index + 1]['value']
                max_index += 1

            while (min_index < max_index and
                   values[min_index]['datetime'] < period_start):

                sum_values -= values[min_index]['value']
                min_index += 1

            max_values.append({
                    'value': sum_values, 'datetime': period_counter})

            period_counter += one_hour

        return max_values

    def rain_stats(self, values, td, start_date, end_date):
        """
        Calculate stats.

        TODO: make faster by caching the data.

        TODO: add herhalingstijd. needs: bui_duur [uren], oppervlakte
        [vierkante km] neerslag_som [mm]
        """
        logger.debug('Calculating rain stats for start=%s, end=%s, td=%s' %
                     (start_date, end_date, td))
        if not values:
            return {
                'td': td,
                'max': '-',
                'datetime': '-',
                'start': '-',
                'end': '-',
                't': '-'}

        # Make start_date and end_date tz aware
        start_date = self._tzaware(
            start_date,
            tzinfo=values[0]['datetime'].tzinfo)
        end_date = self._tzaware(
            end_date,
            tzinfo=values[0]['datetime'].tzinfo)

        max_values = self._max_values(values, td, start_date, end_date)

        if max_values:
            max_value = max(max_values, key=lambda i: i['value'])
            max_value['datetime_end'] = max_value['datetime'] + td
            hours = td.days * 24 + td.seconds / 3600.0
            t = herhalingstijd(hours, 25, max_value['value'])
        else:
            max_value = {'value': '-', 'datetime': '-', 'datetime_end': '-'}
            t = '-'

        return {
            'td': td,
            'max': max_value['value'],
            'start': max_value['datetime'],
            'end': max_value['datetime_end'],
            't': t}

    def html(self, snippet_group=None, identifiers=None, layout_options=None):
        """
        Popup with graph - table - bargraph.
        """
        if snippet_group:
            identifiers = [
                snippet.identifier
                for snippet in snippet_group.snippets.filter(visible=True)]
        title = 'RainApp (%s)' % ', '.join(
            [identifier['location'] for identifier in identifiers])

        # Make table with these identifiers.
        # Layer options contain request - not the best way but it works.
        start_date, end_date = current_start_end_dates(
            layout_options['request'])

        # Contains list of tables. A table is a list of rows/dicts
        # with 'max', 'start', 'end', '..'
        rain_stats = {}

        td_range = [
            datetime.timedelta(days=2),
            datetime.timedelta(days=1),
            datetime.timedelta(hours=3),
            datetime.timedelta(hours=1)]
        for identifier in identifiers:
            values = self._cached_values(identifier, start_date, end_date)
            rain_stats[identifier['location']] = []
            for td in td_range:
                rain_stats[identifier['location']].append(self.rain_stats(
                        values, td,
                        start_date, end_date))

        # Collect urls for graphs.
        symbol_url = self.symbol_url()
        if snippet_group:
            # Image url for snippet_group: can change if snippet_group
            # properties are altered.
            img_url = reverse(
                "lizard_map.snippet_group_image",
                kwargs={'snippet_group_id': snippet_group.id},
                )
            bar_url = reverse(
                "lizard_rainapp.snippet_group_rainapp_bars",
                kwargs={'snippet_group_id': snippet_group.id},
                )
        else:
            # Image url: static url composed with all options and
            # layout tweak.
            img_url = reverse(
                "lizard_map.workspace_item_image",
                kwargs={'workspace_item_id': self.workspace_item.id},
                )
            bar_url = reverse(
                "lizard_rainapp.workspace_item_rainapp_bars",
                kwargs={'workspace_item_id': self.workspace_item.id},
                )
            identifiers_escaped = [json.dumps(identifier).replace('"', '%22')
                                   for identifier in identifiers]
            url_extra = '?' + '&'.join(['identifier=%s' % i for i in
                                        identifiers_escaped])
            img_url += url_extra
            bar_url += url_extra

        return render_to_string(
            'lizard_rainapp/popup_rainapp.html',
            {'title': title,
             'rain_stats': rain_stats,
             'number_of_locations': len(rain_stats.keys()),
             'symbol_url': symbol_url,
             'img_url': img_url,
             'bar_url': bar_url})
