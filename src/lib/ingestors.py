from delta.tables import DeltaTable
from pyspark.sql.window import Window
from pyspark.sql.functions import row_number, desc, col
import sys

# dbutils = DBUtils(spark)
# sys.path.append(f'/Workspace/Users/{dbutils.widgets.get("account")}/music_data_lake/src/lib')
import utils
import preprocessing_silver


class GenericIngestor:
    def __init__(self, spark, config):
        self.spark = spark
        self.config = config
        self.catalog = config["catalog"]
        self.schema = config["schema"]
        self.tablename = config["tablename"]
        self.primary_key = config["primary_key"]
        self.timestamp_field = config["timestamp_field"]
        self.table_path = f"{self.catalog}.{self.schema}.{self.tablename}"

    def execute_full_load(self):

        if not utils.table_exists(self.spark, catalog=self.catalog, schema=self.schema, table=self.tablename):
            df_full = self.spark.read.format("parquet").load(f'/Volumes/raw/{self.schema}/full_load/{self.tablename}/')
            df_full.coalesce(1).write.format("delta").mode("overwrite").saveAsTable(self.table_path)

    def upsert(self, df):
        deltatable = DeltaTable.forName(self.spark, self.table_path)

        df_filtered = df.filter(f"{self.primary_key} IS NOT NULL")
        windowSpec = Window.partitionBy(self.primary_key).orderBy(col(self.timestamp_field).desc())

        df_cdc = (df_filtered.withColumn("row_number", row_number().over(windowSpec))
                            .filter(col("row_number") == 1)
                            .drop("row_number"))

        (deltatable.alias("target")
                    .merge(df_cdc.alias("source"), f"target.{self.primary_key} = source.{self.primary_key}")
                    .whenMatchedDelete(condition="source.operation = 'd'")
                    .whenMatchedUpdateAll(condition="source.operation = 'u'")
                    .whenNotMatchedInsertAll(condition="source.operation = 'c' OR source.operation = 'u'")
                    .execute())

    def process_stream(self, df):
        """Agora a transformação ocorre antes de chamar esta função."""
        stream = (df.writeStream
                  .option("checkpointLocation", f"/Volumes/raw/{self.schema}/cdc/postgres.public.{self.tablename}/{self.tablename}_checkpoint/")
                  .foreachBatch(lambda df, batch_id: self.upsert(df))
                  .trigger(availableNow=True))
        return stream
    

class SilverIngestor(GenericIngestor):
    def __init__(self, spark, config):
        super().__init__(spark, config)
        self.account = config["account"]
        self.id_field_old = config["id_field_old"]
        self.set_query()
        self.checkpoint_location = f"/Volumes/raw/{config['schema']}/cdc/postgres.public.{config['tablename']}/{config['tablename']}_checkpoint_silver/"

    def set_deltatable(self):
        tablename = f"{self.catalog}.{self.schema}.{self.tablename}"
        print(f"tablename -> {tablename}")
        deltatable = DeltaTable.forName(self.spark, tablename)
        return deltatable

    def set_query(self):
        path = f"/Workspace/Users/{self.account}/music_data_lake/src/silver/{self.tablename}.sql"

        with open(path, "r") as open_file:
            query = open_file.read()
        self.from_table = utils.extract_from(query=query)
        self.original_query = query
        self.query = utils.format_query_cdf(query, "{df}")

    def load_cdf(self):
        """Lê os dados da Bronze ativando Change Data Feed (CDF)."""
        return (self.spark.readStream
                .format("delta")
                .option("readChangeFeed", "true") # Ativa CDF
                .option("startingVersion", 0)  # Add this for first-run or full-reprocessing
                .table(self.from_table))
    
    def upsert(self, df):
        deltatable = self.set_deltatable()

        df.createOrReplaceGlobalTempView(f"silver_{self.tablename}")

        query_last = f"""
        SELECT *
        FROM global_temp.silver_{self.tablename}
        WHERE _change_type <> 'update_preimage'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY {self.id_field_old} ORDER BY _commit_timestamp DESC) = 1
        """
        df_last = self.spark.sql(query_last)
        df_upsert = self.spark.sql(self.query, df=df_last)

        df_processed = preprocessing_silver.transform_data(self.tablename, df_upsert)

        # Executar MERGE com base no _change_type
        (deltatable.alias("s")
             .merge(df_processed.alias("d"), f"s.{self.primary_key} = d.{self.primary_key}")
             .whenMatchedDelete(condition="d._change_type = 'delete'")  # Remove registros deletados
             .whenMatchedUpdateAll(condition="d._change_type = 'update_postimage'")  # Atualiza se for update
             .whenNotMatchedInsertAll(condition="d._change_type = 'insert' OR d._change_type = 'update_postimage'")  # Insere novos registros
             .execute())
        
    def process_stream(self, df):
        """
        Processa os dados em streaming aplicando a função upsert em cada batch.
        """
        return (df.writeStream
                 .option("checkpointLocation", self.checkpoint_location)
                 .foreachBatch(lambda df, batch_id: self.upsert(df))
                 .trigger(availableNow=True)
                 .start())  

    def run(self):
        """Executa o pipeline Silver com CDF."""
        df_stream = self.load_cdf()  # Lê as mudanças na Bronze
        return self.process_stream(df_stream)
