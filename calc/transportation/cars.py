import pandas as pd
from calc import calcfunc
from calc.bass import generate_bass_diffusion
from calc.population import predict_population
from calc.electricity import predict_electricity_emission_factor
from calc.transportation.datasets import prepare_transportation_emissions_dataset
from calc.transportation.modal_share import predict_road_mileage
from calc.transportation.car_fleet import predict_cars_in_use_by_engine_type


"""
@calcfunc(
    variables=[
        'target_year', 'municipality_name', 'cars_mileage_per_resident_adjustment'
    ],
    funcs=[predict_population, prepare_transportation_emissions_dataset],
)
def predict_cars_mileage_old(variables):
    target_year = variables['target_year']
    mileage_adj = variables['cars_mileage_per_resident_adjustment']

    df = prepare_transportation_emissions_dataset()
    df = df.loc[df.Vehicle == 'Cars', ['Year', 'Road', 'Mileage', 'CO2e']].set_index('Year')
    df = df.pivot(columns='Road', values='Mileage')
    df.columns = df.columns.astype(str)
    df['Forecast'] = False
    last_historical_year = df.index.max()
    df = df.reindex(range(df.index.min(), target_year + 1))
    pop_df = predict_population()
    df['Population'] = pop_df['Population']
    df['UrbanPerResident'] = df['Urban'] / df['Population']
    df['HighwaysPerResident'] = df['Highways'] / df['Population']
    df.loc[df.Forecast.isna(), 'Forecast'] = True
    for road in ('Highways', 'Urban'):
        s = df[road + 'PerResident'].copy()
        target_per_resident = s.loc[last_historical_year] * (1 + (mileage_adj / 100))
        s.loc[target_year] = target_per_resident
        s = s.interpolate()
        df.loc[df.Forecast, road + 'PerResident'] = s
        df.loc[df.Forecast, road] = df[road + 'PerResident'] * df['Population']

    df['Mileage'] = df['Highways'] + df['Urban']
    df['PerResident'] = df['UrbanPerResident'] + df['HighwaysPerResident']
    df.Forecast = df.Forecast.astype(bool)

    return df
"""


@calcfunc(
    funcs=[predict_road_mileage, predict_population],
)
def predict_cars_mileage():
    mdf = predict_road_mileage()
    df = pd.DataFrame(mdf.pop('Forecast'))
    for vehicle, road in list(mdf.columns):
        if vehicle == 'Cars':
            df[road] = mdf[(vehicle, road)]

    pop_df = predict_population()

    df['Population'] = pop_df['Population']
    df['UrbanPerResident'] = df['Urban'] / df['Population']
    df['HighwaysPerResident'] = df['Highways'] / df['Population']
    df['Mileage'] = df['Highways'] + df['Urban']
    df['PerResident'] = df['UrbanPerResident'] + df['HighwaysPerResident']

    return df


EURO_MODEL_YEARS = (
    1993, 1997, 2001, 2006, 2011, 2013, 2015
)


def estimate_mileage_ratios(df):
    df = df.stack('ModelYear')
    df = df.reset_index('ModelYear')

    model_year_map = {}
    for year in df.ModelYear.unique():
        for idx, class_year in enumerate(EURO_MODEL_YEARS):
            if year < class_year:
                model_year_map[year] = 'EURO %d' % idx
                break
        else:
            model_year_map[year] = 'EURO 6'

    df['EmissionClass'] = df.pop('ModelYear').map(model_year_map)
    df = df.reset_index().rename(columns=dict(BEV='electric', PHEV='PHEV (gasoline)', other='gas'))
    df.columns.name = 'Engine'
    df = df.rename(columns=dict(index='Year')).groupby(['Year', 'EmissionClass']).sum()

    total = df.sum(axis=1).sum(axis=0, level='Year')
    df = df.div(total, axis=0, level='Year')

    df = df.unstack('EmissionClass').stack('Engine')

    return df


def estimate_bev_unit_emissions(unit_emissions, kwh_emissions):
    energy_consumption = dict(
        Highways=0.2,
        Urban=0.17
    )  # kWh/km

    df = pd.DataFrame(kwh_emissions)
    df['Highways'] = df['EmissionFactor'] * energy_consumption['Highways']
    df['Urban'] = df['EmissionFactor'] * energy_consumption['Urban']
    df.index.name = 'Year'
    df = df.drop(columns='EmissionFactor').reset_index().melt(id_vars='Year')
    df = df.rename(columns=dict(variable='Road', value='EURO 6'))
    df['Engine'] = 'electric'
    df = df.set_index(['Road', 'Engine'])

    df = unit_emissions.append(df, sort=True)
    df = df.reset_index().set_index(['Year', 'Road', 'Engine'])
    df = df.unstack(['Road', 'Engine']).fillna(method='pad')

    for emission_class in df.columns.levels[0]:
        for road_type in df.columns.levels[1]:
            bev = df[(emission_class, road_type, 'electric')]
            gas = df[(emission_class, road_type, 'gasoline')]
            df[(emission_class, road_type, 'PHEV (gasoline)')] = (bev + gas) / 2

    df = df.reindex(sorted(df.columns), axis=1)

    return df


def calculate_co2e_per_engine_type(mileage, ratios, unit_emissions):
    df = ratios.unstack('Engine')

    df_h = df.multiply(mileage['Highways'], axis='index')
    df_r = df.multiply(mileage['Urban'], axis='index')
    df_h['Road'] = 'Highways'
    df_r['Road'] = 'Urban'

    df = df_h.append(df_r).reset_index().set_index(['Year', 'Road']).unstack('Road')
    df.columns = df.columns.reorder_levels([0, 2, 1])

    unit_emissions.columns.names = ('EmissionClass', 'Road', 'Engine')

    last_hist_year = mileage[~mileage.Forecast].index.max()

    df = df * unit_emissions
    df.loc[df.index > last_hist_year].fillna(0)
    df = df.stack('Road')

    df = (df.sum(axis=1) / 1000000000).unstack('Road')
    df = df.loc[df.index > last_hist_year]
    mileage.loc[mileage.index > last_hist_year, 'HighwaysEmissions'] = df['Highways']
    mileage.loc[mileage.index > last_hist_year, 'UrbanEmissions'] = df['Urban']
    mileage = mileage.interpolate()

    return mileage


@calcfunc(
    datasets=dict(
        mileage_per_engine_type='jyrjola/lipasto/mileage_per_engine_type',
        car_unit_emissions='jyrjola/lipasto/car_unit_emissions',
    ),
    variables=[
        'target_year', 'municipality_name',
    ],
    funcs=[
        predict_electricity_emission_factor,
        predict_cars_mileage,
        prepare_transportation_emissions_dataset,
        predict_cars_in_use_by_engine_type
    ]
)
def predict_cars_emissions(datasets, variables):
    target_year = variables['target_year']

    mileage_per_engine_type = datasets['mileage_per_engine_type']
    mileage_share_per_engine_type = mileage_per_engine_type.set_index(['Vehicle', 'Engine']).drop(columns='Sum')

    df = prepare_transportation_emissions_dataset()
    df = df.loc[df.Vehicle == 'Cars', ['Year', 'CO2e', 'Road']].set_index('Year')
    emissions_df = df.pivot(values='CO2e', columns='Road')

    df = predict_cars_mileage()
    for road in ('Highways', 'Urban'):
        df[road + 'Emissions'] = emissions_df[road] / 1000  # -> kt

    car_unit_emissions = datasets['car_unit_emissions'].set_index(['Engine', 'Road'])
    elec_df = predict_electricity_emission_factor()
    share_df = predict_cars_in_use_by_engine_type()

    last_hist_year = df[~df.Forecast].index.max()

    # Estimate mileage ratio shares between engine types
    share = estimate_mileage_ratios(share_df)

    # Estimate emissions per km per engine type
    unit_df = car_unit_emissions.reset_index()

    unit_df = unit_df.groupby(['Road', 'Engine', 'Class']).mean()['CO2e'].unstack('Class')
    unit_df['Year'] = last_hist_year
    elec_df = elec_df.loc[elec_df.index >= last_hist_year]
    unit_df = estimate_bev_unit_emissions(unit_df, elec_df['EmissionFactor'])

    df = calculate_co2e_per_engine_type(df, share, unit_df)
    engine_shares = share.sum(axis=1).unstack('Engine')
    for engine_type in ('gasoline', 'diesel', 'electric', 'PHEV (gasoline)'):
        df[engine_type] = engine_shares[engine_type]

    df['Emissions'] = (df['HighwaysEmissions'] + df['UrbanEmissions'])
    df['EmissionFactor'] = df['Emissions'] / (df['Urban'] + df['Highways']) * 1000000000  # g/km

    return df


if __name__ == '__main__':
    df = predict_cars_emissions(skip_cache=True)
    print(df)
